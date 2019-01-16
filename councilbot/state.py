import base64
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

from datetime import datetime, timedelta

import toml

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
        self._polldir = self._statedir / "polls"
        self._polldir.mkdir(parents=True, exist_ok=True)
        self._membersdir = self._statedir / "members"
        self._membersdir.mkdir(parents=True, exist_ok=True)

        self._polls = {}
        self._concluded_polls = {}
        self.reload_polls()

    def _get_rounded_time(self):
        return datetime.utcnow().replace(minute=0, second=0, microsecond=0)

    def expire_polls(self):
        cutoff = self._get_rounded_time()
        for poll_id, (metadata, votes) in list(self._polls.items()):
            if metadata["end_time"] <= cutoff:
                self._conclude_poll(poll_id)

    def autoconclude_polls(self, cutoff=timedelta(hours=-1)):
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

    def reload_polls(self):
        cutoff = self._get_rounded_time().replace(hour=0) - timedelta(days=14)
        self._polls.clear()
        for item in self._polldir.iterdir():
            if (item / DELETED_FLAG_FILE).exists():
                # poll deleted, skip
                continue

            is_concluded = (item / CONCLUDED_FLAG_FILE).exists()
            poll_data = self._load_poll(item)
            if is_concluded:
                if poll_data[0]["end_time"] < cutoff:
                    # do not load polls which are older than 14d in memory
                    continue

                self._concluded_polls[item.name] = poll_data
            else:
                self._polls[item.name] = poll_data

    def _member_vote_file(self, actor):
        return "vote-{}.toml".format(self._member_map[actor]["nick"])

    def _load_poll(self, path):
        with (path / METADATA_FILE).open("r") as f:
            metadata = toml.load(f)

        member_votes = {}
        for actor in self._member_map.keys():
            try:
                with (path / self._member_vote_file(actor)).open("r") as f:
                    member_votes[actor] = toml.load(f)
            except FileNotFoundError:
                member_votes[actor] = {"vote": []}

        return metadata, member_votes

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
        # TODO: delete the tree of a poll when the deletion is confirmed
        logger.debug("confirming transaction %r", transaction)
        if transaction["action"] == "delete":
            path = self._polldir / transaction["revert_data"]["dirname"]
            shutil.rmtree(str(path))

    def _mark_poll_deleted(self, poll_id):
        with (self._polldir / poll_id / DELETED_FLAG_FILE).open("x"):
            pass

    def _mark_poll_concluded(self, poll_id):
        with (self._polldir / poll_id / CONCLUDED_FLAG_FILE).open("x"):
            pass

    def _undelete_poll(self, poll_id):
        (self._polldir / poll_id / DELETED_FLAG_FILE).unlink()

    def _unconclude_poll(self, poll_id):
        (self._polldir / poll_id / CONCLUDED_FLAG_FILE).unlink()

    def _revert_last_cast_vote(self, poll_id, actor):
        actor_info = self._polls[poll_id][1][actor]
        new_actor_info = copy.deepcopy(actor_info)
        new_actor_info["vote"].pop()

        with safe_writer(
                self._polldir / poll_id / self._member_vote_file(actor),
                "w") as f:
            toml.dump(new_actor_info, f)

        actor_info["vote"].pop()

    def _revert_transaction(self, actor, transaction):
        logger.debug("reverting transaction %r", transaction)

        action = transaction["action"]
        if action == "create":
            poll_id = transaction["revert_data"]["dirname"]
            logger.debug("marking %s as deleted and removing from in-memory "
                         "state",
                         poll_id)
            self._mark_poll_deleted(poll_id)
            self._polls.pop(poll_id, None)
        elif action == "delete":
            poll_id = transaction["revert_data"]["dirname"]
            logger.debug("marking %s as undeleted and reloading from disk",
                         poll_id)
            self._undelete_poll(poll_id)
            self._polls[poll_id] = self._load_poll(self._polldir / poll_id)
        elif action == "cast_vote":
            poll_id = transaction["revert_data"]["dirname"]
            self._revert_last_cast_vote(poll_id, actor)
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

    def _conclude_poll(self, poll_id):
        cutoff = self._get_rounded_time()
        metadata, votes = self.get_poll_info(poll_id)
        state = self.get_poll_state(poll_id)
        if state == PollState.OPEN and metadata["end_time"] > cutoff:
            raise ValueError(
                "cannot conclude poll with open votes before expiration"
            )

        new_metadata = copy.deepcopy(metadata)
        if state.is_complete:
            result_state = PollState.CONCLUDED
        elif metadata["end_time"] <= cutoff:
            result_state = PollState.EXPIRED
        else:
            raise ValueError("poll cannot be concluded at this time")

        new_metadata["state"] = result_state.value

        try:
            self._mark_poll_concluded(poll_id)

            with safe_writer(self._polldir / poll_id / METADATA_FILE, "w") as f:
                toml.dump(new_metadata, f)
        except:  # NOQA
            try:
                self._unconclude_poll(poll_id)
            except FileNotFoundError:
                pass

            with safe_writer(self._polldir / poll_id / METADATA_FILE,
                             "w") as f:
                toml.dump(metadata, f)

            raise

        metadata["state"] = new_metadata["state"]
        self._concluded_polls[poll_id] = self._polls.pop(poll_id)
        self.on_poll_concluded(poll_id, result_state.conclusion_reason)

    def create_poll(self,
                    actor,
                    message_id,
                    topic,
                    lifetime=timedelta(days=14)) -> TransactionID:
        # data for reversal: dirname (to mark deleted)
        tid = self.make_transaction_id()

        start_time = self._get_rounded_time() + timedelta(hours=1)

        dirname = "{:%Y-%m-%d}-{}-{}".format(
            start_time,
            tid,
            slugify(topic)[:50]
        )

        metadata = {
            "start_time": start_time,
            "end_time": start_time + lifetime,
            "dirname": dirname,
            "topic": topic,
            "actor": str(actor),
        }

        try:
            path = self._polldir / dirname
            path.mkdir()

            with (path / METADATA_FILE).open("w") as f:
                toml.dump(metadata, f)

            self.write_last_transaction(
                actor,
                message_id,
                tid,
                action="create",
                revert_data={"dirname": dirname},
            )
        except:  # NOQA
            try:
                (path / METADATA_FILE).unlink()
            except FileNotFoundError:
                pass

            try:
                path.rmdir()
            except FileNotFoundError:
                pass

            raise

        self._polls[dirname] = (
            metadata,
            {},
        )

        return tid, dirname

    def cast_vote(self,
                  actor,
                  message_id,
                  poll_id,
                  value,
                  remark) -> TransactionID:
        self.expire_polls()

        # data for reversal: dirname
        tid = self.make_transaction_id()

        new_vote = {
            "value": value.value,
            "remark": remark,
            "timestamp": datetime.utcnow(),
        }

        try:
            actor_info = self._polls[poll_id][1][actor]
        except KeyError:
            actor_info = self._polls[poll_id][1][actor] = {"vote": []}

        new_actor_info = copy.deepcopy(actor_info)
        new_actor_info["vote"].append(new_vote)

        with safe_writer(
                self._polldir / poll_id / self._member_vote_file(actor),
                "w") as f:
            toml.dump(new_actor_info, f)

        self.write_last_transaction(
            actor,
            message_id,
            tid,
            "cast_vote",
            revert_data={"dirname": poll_id},
        )

        actor_info["vote"].append(new_vote)

        return tid

    def delete_poll(self, actor, message_id, poll_id) -> TransactionID:
        # data for reversal: dirname; the directory is not deleted right away,
        # only when the deletion transaction becomes irreversible
        tid = self.make_transaction_id()

        self._mark_poll_deleted(poll_id)

        self.write_last_transaction(
            actor,
            message_id,
            tid,
            action="delete",
            revert_data={"dirname": poll_id},
        )

        self._polls.pop(poll_id, None)

        return tid

    def rename_poll(self,
                    actor,
                    message_id,
                    poll_id,
                    new_topic) -> TransactionID:
        # data for reversal: dirname, old_topic
        tid = self.make_transaction_id()

    def find_poll(self, text) -> str:
        text = text.casefold()
        pollmap = [
            (metadata["topic"], metadata["dirname"])
            for metadata, _ in self._polls.values()
        ]
        options = [item[0].casefold() for item in pollmap]
        match = difflib.get_close_matches(text, options, n=1, cutoff=0.4)
        if not match:
            raise KeyError(text)

        match, = match

        return pollmap[options.index(match)][1]

    def get_poll_info(self, poll_id):
        self.expire_polls()
        try:
            return self._polls[poll_id]
        except KeyError:
            return self._concluded_polls[poll_id]

    def get_poll_state(self, poll_id):
        self.expire_polls()

        now = self._get_rounded_time()
        nmembers = len(self._member_map)
        metadata, votes = self.get_poll_info(poll_id)

        try:
            written_state = PollState(metadata["state"])
        except KeyError:
            pass
        else:
            return written_state

        is_complete = (
            len(votes) == nmembers and
            sum(bool(member_info.get("vote", []))
                for member_info in votes.values()) == nmembers
        )
        is_expired = now >= metadata["end_time"]

        if is_complete and is_expired:
            return PollState.CONCLUDED
        elif is_complete:
            return PollState.COMPLETE
        elif is_expired:
            return PollState.EXPIRED
        else:
            return PollState.OPEN

    def get_vote_summary(self, poll_id):
        _, votes = self.get_poll_info(poll_id)

        has_veto = False
        number_of_votes = 0
        number_of_acks = 0
        number_of_members = len(self._member_map)
        quorum = math.ceil(number_of_members / 2)

        member_vote_map = {}

        for actor in self._member_map:
            vote_info = (votes.get(actor, {}).get("vote") or [None])[-1]
            if vote_info is None:
                member_vote_map[actor] = None
                continue

            value = VoteValue(vote_info["value"])
            number_of_votes += 1
            if value == VoteValue.VETO:
                has_veto = True
            elif value == VoteValue.ACK:
                number_of_acks += 1

            member_vote_map[actor] = {
                "value": value,
                "remark": vote_info["remark"] or None
            }

        if has_veto:
            result = PollResult.VETO
        elif number_of_acks >= quorum:
            result = PollResult.PASS
        else:
            result = PollResult.FAIL

        return {
            "result": result,
            "complete": number_of_votes == number_of_members,
            "votes": member_vote_map,
        }

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
