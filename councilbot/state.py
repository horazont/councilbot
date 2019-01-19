import base64
import collections
import contextlib
import copy
import difflib
import enum
import logging
import math
import os
import pathlib
import random
import re
import shutil
import tempfile
import typing

from datetime import datetime, timedelta

import toml

import aioxmpp
import aioxmpp.callbacks


logger = logging.getLogger(__name__)


TransactionID = str
_rng = random.SystemRandom()
CONCLUDED_FLAG_FILE = "concluded.flag"
DELETED_FLAG_FILE = "deleted.flag"
METADATA_FILE = "metadata.toml"


class VoteValue(enum.Enum):
    VETO = "-1"
    MINUS_ZERO = "-0"
    PLUS_ZERO = "+0"
    ACK = "+1"


class PollResult(enum.Enum):
    FAIL = "fail"
    VETO = "veto"
    PASS = "pass"

    @property
    def has_passed(self):
        return self == PollResult.PASS

    @property
    def has_veto(self):
        return self == PollResult.VETO


class PollState(enum.Enum):
    OPEN = "open"
    COMPLETE = "complete"
    CONCLUDED = "concluded"
    EXPIRED = "expired"

    @property
    def is_concluded(self):
        return self in [PollState.CONCLUDED, PollState.EXPIRED]

    @property
    def is_expired(self):
        return self == PollState.EXPIRED

    @property
    def is_complete(self):
        return self in [PollState.CONCLUDED, PollState.COMPLETE]

    @property
    def is_open(self):
        return self in [PollState.OPEN, PollState.COMPLETE]

    @property
    def conclusion_reason(self):
        return self._REASON_MAP.get(self, None)


class PollFlag(enum.Enum):
    # set on a poll when its conclusion has been officially announced
    CONCLUDED = "concluded"


class ConclusionReason(enum.Enum):
    VOTES_CAST = "votes cast"
    EXPIRATION = "expiration"

    def to_poll_state(self):
        return self._STATE_MAP[self]


PollState._REASON_MAP = {
    PollState.CONCLUDED: ConclusionReason.VOTES_CAST,
    PollState.EXPIRED: ConclusionReason.EXPIRATION,
}

ConclusionReason._STATE_MAP = {
    ConclusionReason.VOTES_CAST: PollState.CONCLUDED,
    ConclusionReason.EXPIRATION: PollState.EXPIRED,
}


def slugify(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9A-Z]", "-", text.casefold()))


def fsync_dir(path: pathlib.Path):
    """
    Call :func:`os.fsync` on a directory.

    :param path: The directory to fsync.
    """
    fd = os.open(str(path), os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def safe_writer(destpath, mode="wb", extra_paranoia=False):
    """
    Safely overwrite a file.

    This guards against the following situations:

    * error/exception while writing the file (the original file stays intact
      without modification)
    * most cases of unclean shutdown (*either* the original *or* the new file
      will be seen on disk)

    It does that with the following means:

    * a temporary file next to the target file is used for writing
    * if an exception is raised in the context manager, the temporary file is
      discarded and nothing else happens
    * otherwise, the temporary file is synced to disk and then used to replace
      the target file.

    If `extra_paranoia` is true, the parent directory of the target file is
    additionally synced after the replacement. `extra_paranoia` is only needed
    if it is required that the new file is seen after a crash (and not the
    original file).
    """

    destpath = pathlib.Path(destpath)
    with tempfile.NamedTemporaryFile(
            mode=mode,
            dir=str(destpath.parent),
            delete=False) as tmpfile:
        try:
            yield tmpfile
        except:  # NOQA
            os.unlink(tmpfile.name)
            raise
        else:
            tmpfile.flush()
            os.fsync(tmpfile.fileno())
            os.replace(tmpfile.name, str(destpath))
            if extra_paranoia:
                fsync_dir(destpath.parent)


class VoteRecord(collections.namedtuple("VoteRecord",
                                        [
                                            "timestamp",
                                            "value",
                                            "remark",
                                        ])):
    def to_dict(self) -> typing.Mapping:
        return {
            "timestamp": self.timestamp,
            "value": self.value.value,
            "remark": self.remark,
        }

    @classmethod
    def from_dict(cls, d: typing.Mapping):
        return cls(
            d["timestamp"],
            VoteValue(d["value"]),
            d.get("remark"),
        )


class Poll:
    """
    Represent a poll.

    .. autoattribute:: id_

    .. attribute:: subject

    .. autoattribute:: start_time

    .. autoattribute:: end_time

    .. autoattribute:: state

    .. autoattribute:: result

    .. autoattribute:: flags

    .. attribute:: urls

    .. attribute:: tag

    .. attribute:: description

    .. automethod:: push_vote

    .. automethod:: pop_vote

    .. automethod:: get_vote_history

    .. automethod:: get_current_votes
    """

    def __init__(self, id_, start_time, duration, subject, members):
        super().__init__()
        self._id = id_
        self._start_time = start_time
        self._end_time = self._start_time + duration
        self._flags = set()
        self._member_data = {
            member: []
            for member in members
        }
        self.subject = subject
        self.tag = None
        self.urls = []
        self.description = None

    def __copy__(self):
        result = type(self)(self._id,
                            self._start_time,
                            self._end_time - self._start_time,
                            self.subject,
                            [])
        result._flags.update(self._flags)
        for member, votes in self._member_data.items():
            result._member_data[member] = copy.copy(votes)
        result.tag = self.tag
        result.urls[:] = self.urls
        result.description = self.description
        return result

    @property
    def id_(self) -> str:
        return self._id

    @property
    def start_time(self) -> datetime:
        return self._start_time

    @property
    def end_time(self) -> datetime:
        return self._end_time

    @property
    def result(self) -> PollResult:
        # This function implements the last paragraph fo Section 8.1 of the
        # XSF Bylaws.

        # XSF Bylaws section 8.1
        #
        # > The XMPP Council shall act upon the affirmative vote of a majority
        # > of the members of the Council voting, although the negative vote of
        # > any one member of the Council shall function as a veto. A quorum of
        # > the XMPP Council shall be a majority of the members of the Council.

        number_of_acks = 0
        number_of_votes = 0

        for vote in self.get_current_votes().values():
            if vote is None:
                continue

            if vote.value == VoteValue.VETO:
                # Bylaws:
                # > […] although the negative vote of any one member of the
                # > Council shall function as a veto.
                return PollResult.VETO

            number_of_votes += 1
            if vote.value == VoteValue.ACK:
                number_of_acks += 1

        # Bylaws:
        # > A quorum of the XMPP Council shall be a majority of the members of
        # > the Council.

        # number of council members = len(self._member_data)
        quorum = len(self._member_data) / 2

        if number_of_votes <= quorum:
            # no quorum -> fail
            return PollResult.FAIL

        # Bylaws:
        # > The XMPP Council shall act upon the affirmative vote of a majority
        # > of the members of the Council voting […].

        # number of the members of the Council voting = number_of_votes
        majority = number_of_votes / 2

        if number_of_acks <= majority:
            # no majority -> fail
            return PollResult.FAIL

        return PollResult.PASS

    @property
    def flags(self) -> typing.Set[PollFlag]:
        return self._flags

    def get_state(self, at_time: datetime) -> PollState:
        is_complete = all(bool(votes) for votes in self._member_data.values())

        if at_time >= self._end_time:
            if is_complete:
                return PollState.CONCLUDED
            return PollState.EXPIRED

        if is_complete:
            return PollState.COMPLETE

        return PollState.OPEN

    def push_vote(self,
                  member: aioxmpp.JID,
                  value: VoteValue,
                  remark: typing.Optional[str],
                  timestamp: datetime = None):
        """
        Push a vote to the members voting history on this poll.
        """
        if timestamp is None:
            timestamp = datetime.utcnow()

        record = VoteRecord(timestamp, value, remark)
        records = self._member_data[member]
        records.append(record)

    def pop_vote(self, member: aioxmpp.JID):
        """
        Remove the latest vote of the given member from this poll.

        (Used primarily to process last message corrections.)
        """
        records = self._member_data[member]
        if not records:
            return

        records.pop()

    def get_vote_history(self) -> typing.Mapping[
            aioxmpp.JID, typing.List[VoteRecord]]:
        """
        Get mapping of vote history by member.
        """
        return self._member_data

    def get_votes(self,
                  member: aioxmpp.JID) -> typing.List[VoteRecord]:
        """
        Get vote history of member.
        """
        return self._member_data[member]

    def get_current_votes(self) -> typing.Mapping[aioxmpp.JID, VoteRecord]:
        """
        Get mapping with most recent votes by member.
        """
        return {
            member: votes[-1] if len(votes) > 0 else None
            for member, votes in self._member_data.items()
        }

    def dump(self, fout):
        data = {
            "id": self._id,
            "start_time": self._start_time,
            "end_time": self._end_time,
            "subject": self.subject,
            "flags": list(flag.value for flag in self._flags),
            "tag": self.tag,
            "urls": self.urls,
            "votes": {
                str(member): [
                    vote.to_dict()
                    for vote in votes
                ]
                for member, votes in self._member_data.items()
            },
        }
        if self.description is not None:
            data["description"] = self.description

        toml.dump(data, fout)

    @classmethod
    def load(cls, fin):
        data = toml.load(fin)
        result = cls(
            data["id"],
            data["start_time"],
            data["end_time"] - data["start_time"],
            data["subject"],
            map(aioxmpp.JID.fromstr, data["votes"].keys()),
        )

        result._flags.update(
            PollFlag(flag)
            for flag in data["flags"]
        )

        for member, votes in data["votes"].items():
            member = aioxmpp.JID.fromstr(member)
            records = result._member_data[member]
            records[:] = map(VoteRecord.from_dict, votes)

        result.tag = data.get("tag")
        result.urls[:] = data.get("urls", [])
        result.description = data.get("description")

        return result


class State:
    on_poll_concluded = aioxmpp.callbacks.Signal()

    def __init__(self, config):
        super().__init__()
        self._member_map = {
            member["address"]: member
            for member in config["council"]["members"]
        }
        self._member_state_cache = {}
        self._statedir = pathlib.Path(config["state"]["directory"]).resolve()
        self._activedir = self._statedir / "polls" / "active"
        self._activedir.mkdir(parents=True, exist_ok=True)
        self._archivedir = self._statedir / "polls" / "archive"
        self._archivedir.mkdir(parents=True, exist_ok=True)
        self._trashdir = self._statedir / "polls" / "trash"
        self._trashdir.mkdir(parents=True, exist_ok=True)
        self._membersdir = self._statedir / "members"
        self._membersdir.mkdir(parents=True, exist_ok=True)
        self._agendadir = self._statedir / "agenda"
        self._agendadir.mkdir(parents=True, exist_ok=True)

        self._polls = {}
        self.reload_polls()

    def _get_rounded_time(self):
        return datetime.utcnow().replace(minute=0, second=0, microsecond=0)

    def expire_polls(self):
        cutoff = self._get_rounded_time()
        for poll_id, poll in list(self._polls.items()):
            state = poll.get_state(cutoff)
            if state.is_concluded:
                self._conclude_poll(poll)

    def autoconclude_polls(self, cutoff=timedelta(hours=-1)):
        raise NotImplementedError

        # the last vote must have been cast before the cutoff, to give everyone
        # a chance to correct
        cutoff = datetime.utcnow() - cutoff
        logger.debug("autoconclude requested (cutoff=%s)", cutoff)
        nmembers = len(self._member_map)

        any_concluded = False
        for poll_id, (metadata, votes) in list(self._polls.items()):
            if len(votes) < nmembers:
                logger.debug("%s does not qualify: not all members voted yet",
                             poll_id)
                # doesn’t have votes from all members yet
                continue
            if not all(member_info.get("vote")
                       for member_info in votes.values()):
                logger.debug("%s does not qualify: not all members voted yet",
                             poll_id)
                # same, but with different data structure (can happen e.g. if
                # a member LMC’s a vote without creating a new vote in the
                # process)
                continue
            if any(member_info["vote"][-1]["timestamp"] > cutoff
                   for member_info in votes.values()):
                logger.debug("%s does not qualify: newest vote isn't old "
                             "enough", poll_id)
                # the most recent vote isn’t old enough yet
                continue

            logger.debug("auto-concluding %s", poll_id)
            self._conclude_poll(poll_id)
            any_concluded = True

        return any_concluded

    def _poll_filename(self, id_):
        return "{}.toml".format(id_)

    def _archive_poll(self, id_):
        logger.debug("archiving poll: %s", id_)
        filename = self._poll_filename(id_)
        (self._activedir / filename).rename(self._archivedir / filename)
        self._polls.pop(id_, None)

    def _trash_poll(self, id_):
        logger.debug("trashing poll: %s", id_)
        filename = self._poll_filename(id_)
        (self._activedir / filename).rename(self._trashdir / filename)
        self._polls.pop(id_, None)

    def _unarchive_poll(self, id_):
        logger.debug("recovering poll from archive: %s", id_)
        filename = self._poll_filename(id_)
        active_path = self._activedir / filename
        (self._archivedir / filename).rename(active_path)
        with active_path.open("r") as f:
            self._polls[id_] = Poll.load(f)

    def _untrash_poll(self, id_):
        logger.debug("restoring poll from trash: %s", id_)
        filename = self._poll_filename(id_)
        active_path = self._activedir / filename
        (self._trashdir / filename).rename(active_path)
        with active_path.open("r") as f:
            self._polls[id_] = Poll.load(f)

    def _delete_poll(self, id_):
        logger.debug("deleting poll: %s", id_)
        filename = self._poll_filename(id_)
        (self._trashdir / filename).unlink()

    def reload_polls(self):
        logger.debug("reload_polls: reloading all polls")
        self._polls.clear()
        to_archive = []
        for item in self._activedir.iterdir():
            with item.open("r") as f:
                data = Poll.load(f)

            if PollState.CONCLUDED in data.flags:
                logger.debug(
                    "reload_polls: poll %s is concluded, "
                    "will move to archive later",
                    data.id_,
                )
                # move it to archive in second pass
                to_archive.append(data.id_)
                continue

            self._polls[data.id_] = data
            logger.debug("reload_polls: loaded poll %s", data.id_)

        for id_ in to_archive:
            logger.debug("reload_polls: archiving concluded poll: %s", id_)
            self._archive_poll(id_)

    def make_transaction_id(self):
        return "t{}".format(
            base64.urlsafe_b64encode(
                _rng.getrandbits(120).to_bytes(120//8, "little")
            ).decode("ascii").rstrip("=")
        )

    def _member_file(self, actor):
        return (
            self._membersdir / "{}.toml".format(
                self._member_map[actor]["nick"]
            )
        )

    def _read_member_state(self, actor):
        try:
            return self._member_state_cache[actor]
        except KeyError:
            pass

        try:
            with self._member_file(actor).open("r") as f:
                return toml.load(f)
        except FileNotFoundError:
            return {
                "last_message": {
                    "id": None,
                    "transaction": None,
                }
            }

    def _write_member_state(self, actor, new_state):
        member_file = self._member_file(actor)

        with safe_writer(member_file, "w") as fout:
            toml.dump(new_state, fout)

        self._member_state_cache[actor] = new_state

    def _commit_poll_changes(self, new_obj: Poll):
        with safe_writer(
                self._activedir / self._poll_filename(new_obj.id_),
                "w") as f:
            new_obj.dump(f)

        self._polls[new_obj.id_] = new_obj

    @contextlib.contextmanager
    def _edit_poll(self, poll: typing.Union[Poll, str]) -> Poll:
        if isinstance(poll, Poll):
            poll = copy.copy(poll)
        else:
            poll = self._open_poll_for_writing(poll)

        yield poll
        # on exception, changes are discarded
        self._commit_poll_changes(poll)

    def _rewrite_member_last_message(self, actor,
                                     message_id,
                                     transaction,
                                     reply_id=None):
        state = self._read_member_state(actor)
        prev_transaction = state["last_message"].get("transaction")
        if prev_transaction is not None:
            self._confirm_transaction(prev_transaction)
        state["last_message"]["transaction"] = transaction
        state["last_message"]["message_id"] = message_id
        state["last_message"]["reply_id"] = \
            transaction["tid"] if transaction is not None else reply_id

        # commented out because untested
        # if prev_transaction is None and transaction is None:
        #     # no need to actually re-write the state
        #     return

        self._write_member_state(actor, state)

    def write_last_message_id(self, actor, message_id, reply_id=None):
        self._rewrite_member_last_message(actor, message_id, None, reply_id)

    def write_last_transaction(self, actor, message_id,
                               tid, action, revert_data):
        transaction = {
            "actor": str(actor),
            "tid": tid,
            "action": action,
            "revert_data": revert_data,
        }

        if message_id is None:
            self._confirm_transaction(transaction)
            self.write_last_message_id(actor, message_id)
            return

        self._rewrite_member_last_message(actor, message_id, transaction)

    def _confirm_transaction(self, transaction):
        logger.debug("confirming transaction %r", transaction)
        if transaction["action"] == "delete":
            self._delete_poll(transaction["revert_data"]["id"])

    def _open_poll_for_writing(self, poll_id: str) -> Poll:
        return copy.copy(self._polls[poll_id])

    def _revert_last_cast_vote(self, poll_id, actor):
        with self._edit_poll(poll_id) as poll:
            poll.pop_vote(actor)

    def _revert_transaction(self, actor, transaction):
        logger.debug("reverting transaction %r", transaction)

        action = transaction["action"]
        if action == "create":
            poll_id = transaction["revert_data"]["id"]
            logger.debug("marking %s as deleted and removing from in-memory "
                         "state",
                         poll_id)
            self._trash_poll(poll_id)
        elif action == "delete":
            poll_id = transaction["revert_data"]["id"]
            logger.debug("marking %s as undeleted and reloading from disk",
                         poll_id)
            self._untrash_poll(poll_id)
        elif action == "cast_vote":
            poll_id = transaction["revert_data"]["id"]
            self._revert_last_cast_vote(poll_id, actor)
        elif action == "attach_url":
            poll_id = transaction["revert_data"]["id"]
            url = transaction["revert_data"]["url"]
            with self._edit_poll(poll_id) as poll:
                try:
                    poll.urls.remove(url)
                except ValueError as exc:
                    # not in urls, maybe edited already, ignore
                    logger.debug(
                        "revert attach_url: failed to remove %s from poll "
                        "%s: %s",
                        url,
                        poll_id,
                        exc,
                    )
        else:
            raise RuntimeError("unknown transaction: {!r}".format(transaction))

    def revert_last_transaction(self, actor, message_id):
        logger.debug("revert of transaction triggered by %s from %s requested",
                     message_id, actor)
        member_state = self._read_member_state(actor)
        last_id = member_state["last_message"]["message_id"]
        if last_id != message_id:
            logger.debug("%s is not the last message_id (%s) seen from %s",
                         message_id, last_id, actor)
            return
        transaction = member_state["last_message"]["transaction"]
        if transaction is None:
            logger.debug("no transaction associated with %s", message_id)
            return

        member_state["last_message"]["transaction"] = None

        self._revert_transaction(actor, transaction)
        return transaction["tid"]

    def _conclude_poll(self, poll: Poll):
        cutoff = self._get_rounded_time()
        state = poll.get_state(cutoff)
        if state == PollState.OPEN:
            raise ValueError(
                "cannot conclude poll with open votes before expiration"
            )

        with self._edit_poll(poll) as poll:
            poll.flags.add(PollFlag.CONCLUDED)

            self.on_poll_concluded(
                poll.id_,
                state.conclusion_reason,
            )

    def create_poll(self,
                    actor,
                    message_id,
                    topic,
                    lifetime=timedelta(days=14),
                    tag=None,
                    urls=[],
                    description=None) -> TransactionID:
        # data for reversal: dirname (to mark deleted)
        tid = self.make_transaction_id()

        start_time = self._get_rounded_time() + timedelta(hours=1)

        id_ = "{:%Y-%m-%d}-{}-{}".format(
            start_time,
            tid,
            slugify(topic)[:50]
        )

        poll = Poll(
            id_,
            start_time,
            lifetime,
            topic,
            self._member_map.keys(),
        )
        poll.urls[:] = urls
        poll.tag = tag
        poll.description = description
        path = self._activedir / self._poll_filename(id_)

        try:
            with path.open("x") as f:
                poll.dump(f)

            self.write_last_transaction(
                actor,
                message_id,
                tid,
                action="create",
                revert_data={"id": id_},
            )
        except:  # NOQA
            try:
                path.unlink()
            except FileNotFoundError:
                pass

            raise

        self._polls[id_] = poll

        return tid, id_

    def cast_vote(self,
                  actor: aioxmpp.JID,
                  message_id,
                  poll_id: str,
                  value: VoteValue,
                  remark: typing.Optional[str]) -> TransactionID:
        self.expire_polls()

        # data for reversal: dirname
        tid = self.make_transaction_id()

        with self._edit_poll(poll_id) as poll:
            poll.push_vote(
                actor,
                value,
                remark,
            )

            self.write_last_transaction(
                actor,
                message_id,
                tid,
                "cast_vote",
                revert_data={"id": poll_id},
            )

        return tid

    def attach_url(self,
                   actor: aioxmpp.JID,
                   message_id,
                   poll_id: str,
                   url: str) -> TransactionID:
        tid = self.make_transaction_id()

        with self._edit_poll(poll_id) as poll:
            poll.urls.append(url)

            self.write_last_transaction(
                actor,
                message_id,
                tid,
                "attach_url",
                revert_data={"id": poll_id, "url": url}
            )

        return tid

    def delete_poll(self, actor, message_id, poll_id) -> TransactionID:
        # data for reversal: dirname; the directory is not deleted right away,
        # only when the deletion transaction becomes irreversible
        tid = self.make_transaction_id()

        self._trash_poll(poll_id)

        self.write_last_transaction(
            actor,
            message_id,
            tid,
            action="delete",
            revert_data={"id": poll_id},
        )

        return tid

    def rename_poll(self,
                    actor,
                    message_id,
                    poll_id,
                    new_topic) -> TransactionID:
        # data for reversal: dirname, old_topic
        tid = self.make_transaction_id()

    def _find_poll_in_listmap(self, text, pollmap, confidence) -> str:
        options = [item[0].casefold() for item in pollmap]
        match = difflib.get_close_matches(text, options, n=1,
                                          cutoff=confidence)
        if not match:
            raise KeyError(text)

        match, = match

        return pollmap[options.index(match)][1]

    def find_poll(self, text) -> str:
        text = text.casefold()

        pollmap = [
            (poll.tag, poll.id_)
            for poll in self._polls.values()
            if poll.tag is not None
        ]
        try:
            return self._find_poll_in_listmap(
                text,
                pollmap,
                confidence=0.8
            )
        except KeyError:
            pass

        pollmap = [
            (poll.subject, poll.id_)
            for poll in self._polls.values()
        ]

        return self._find_poll_in_listmap(
            text,
            pollmap,
            confidence=0.4
        )

    def get_poll(self, poll_id: str) -> Poll:
        self.expire_polls()
        return self._polls[poll_id]

    @property
    def active_polls(self):
        self.expire_polls()
        return self._polls.keys()

    def is_council_member(self, actor):
        return actor in self._member_map

    @property
    def members(self):
        return self._member_map.keys()

    def get_member_info(self, actor):
        return self._member_map[actor]
