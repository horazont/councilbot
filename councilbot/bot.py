import asyncio
import functools

from datetime import datetime, timedelta

import babel.dates

import aioxmpp
import aioxmpp.muc
import aioxmpp.service
import aioxmpp.xso

import councilbot.state

from . import parser


class Replace(aioxmpp.xso.XSO):
    TAG = ("urn:xmpp:message-correct:0", "replace")

    id_ = aioxmpp.xso.Attr(
        "id",
    )


aioxmpp.Message.xep0308_replace = aioxmpp.xso.Child([Replace])


def extract_text(body):
    try:
        return body.lookup([aioxmpp.structs.LanguageRange.fromstr("en")])
    except KeyError:
        return body.any()


def is_addressed_to(text, nickname):
    # format is "<nickname>[,:] "
    if len(text) < len(nickname) + 2:
        return False
    if not text.startswith(nickname):
        return False
    if text[len(nickname)] not in ",:":
        return False
    if text[len(nickname)+1] != " ":
        return False
    return True


def partition_request(nickname, text):
    """
    Partition the nickname out of a request text.

    :raises ValueError: if the request text does not start with the nickname
        followed by a valid separator.
    :return: The request text without the addressing part.
    """
    if not is_addressed_to(text, nickname):
        raise ValueError("not addressed to {}".format(nickname))

    return text[len(nickname)+2:].strip()


def log_exceptions(logger, message=None):
    def decorator(f):
        nonlocal message
        if message is None:
            message = "{} failed".format(f)

        @functools.wraps(f)
        def func(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception:
                logger.error(message, exc_info=True)

        return func

    return decorator


class CouncilBot(aioxmpp.service.Service):
    ORDER_AFTER = [
        aioxmpp.MUCClient,
        aioxmpp.RosterClient,
        aioxmpp.PresenceClient,
    ]

    LANGUAGE = aioxmpp.structs.LanguageTag.fromstr("en")

    on_fatal_error = aioxmpp.callbacks.Signal()

    def __init__(self, client, **kwargs):
        super().__init__(client, **kwargs)
        self._muc_client = self.dependencies[aioxmpp.MUCClient]
        self._background_task = None
        self._action_map = {
            parser.Action.NULL: self._action_nothing,
            parser.Action.HELP: self._action_help,

            parser.Action.CREATE_POLL: self._action_create_poll,
            parser.Action.LIST_VOTES: self._action_list_votes,
            parser.Action.CONCLUDE_POLL: self._action_conclude_poll,
            parser.Action.CAST_VOTE: self._action_cast_vote,
            parser.Action.DELETE_POLL: self._action_delete_poll,

            parser.Action.LIST_POLLS: self._action_list_polls,
            parser.Action.AUTO_CONCLUDE_OPEN_POLLS: self._action_autoconclude,
        }
        assert (
            set(self._action_map.keys()) == set(parser.Action),
            "not all actions are declared"
        )

    def set_state_object(self, state: councilbot.state.State):
        self._state = state
        self._state.on_poll_concluded.connect(self._handle_poll_concluded)

    def set_room(self, room, nickname):
        self._room_address = room
        self._nickname = nickname

    def _background_task_done(self, task):
        try:
            result = task.result()
            raise RuntimeError("background task exited early: {!r}".format(
                result
            ))
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            self.logger.error("background task crashed", exc_info=True)
            self.on_fatal_error(exc)

    async def _periodic_expire(self):
        while True:
            await asyncio.sleep(3600)
            self._state.expire_polls()

    @aioxmpp.service.depsignal(aioxmpp.Client, "on_stream_established",
                               defer=True)
    async def _stream_established(self):
        self._room, fut = self._muc_client.join(
            self._room_address,
            self._nickname,
            autorejoin=False,
            history=aioxmpp.muc.xso.History(maxchars=0, maxstanzas=0),
        )
        self._room.on_message.connect(
            log_exceptions(self.logger, "message handler failed")(
                self._handle_council_room_message
            )
        )
        self._room.on_join.connect(self._handle_council_room_join)
        self._room.on_exit.connect(self._handle_council_room_exit)
        try:
            await fut
        except Exception as exc:
            self.on_fatal_error(exc)

        self._state.expire_polls()
        self._background_task = asyncio.ensure_future(
            self._periodic_expire()
        )
        self._background_task.add_done_callback(self._background_task_done)

    @aioxmpp.service.depsignal(aioxmpp.Client, "on_stream_destroyed")
    def _stream_kaputt(self):
        if self._background_task is not None:
            self._background_task.cancel()
            self._background_task = None

    def _send_reply(self, requester, text, *, message_id=None, replace_id=None):
        message = aioxmpp.Message(type_=aioxmpp.MessageType.GROUPCHAT)
        message.id_ = message_id
        if replace_id is not None:
            message.xep0308_replace = Replace()
            message.xep0308_replace.id_ = replace_id
        if requester is not None and requester.nick is not None:
            text = "{}, {}".format(requester.nick, text)
        message.body[self.LANGUAGE] = text
        self._room.send_message(message)

    def _handle_council_room_message(self, message, member, source, **kwargs):
        if self._room.me is member:
            self.logger.debug("ignoring message from myself: %s", message)
            return

        text = extract_text(message.body)
        text = text.strip()
        if text == "ping":
            self._send_reply(member, "pong")
            return

        actor = member.direct_jid

        if not self._state.is_council_member(actor):
            self.logger.debug(
                "ignoring message from non-member %s", actor
            )
            return

        if message.xep0308_replace is not None:
            self.logger.debug(
                "replacement message from member, checking if it matches the "
                "last action"
            )

            replace_id = self._state.revert_last_transaction(
                actor,
                message.xep0308_replace.id_,
            )
        else:
            replace_id = None

        # always note the last message id, to be sure. we have to do this after
        # possible reversals though, because this command discards transaction
        # info
        self._state.write_last_message_id(actor, message.id_)

        try:
            request = partition_request(self._room.me.nick, text)
        except ValueError:
            self.logger.debug(
                "ignoring message %r which isn’t addressed to me (%s)",
                text,
                self._room.me.nick
            )

            if replace_id is not None:
                self._send_reply(None, "nevermind", replace_id=replace_id)

            return

        words = list(filter(None, request.split(" ")))
        info = parser.PARSE_TREE.parse(words)
        if info is None:
            self._send_reply(
                member, "sorry, I did not understand that.",
                replace_id=replace_id
            )
            return

        node, remaining_words, params = info
        action_func = self._action_map[node.action]

        tid, reply = action_func(
            actor,
            message.id_,
            remaining_words,
            params
        )

        if reply is not None:
            self._send_reply(member, reply,
                             message_id=tid,
                             replace_id=replace_id)
        elif replace_id is not None:
            self._send_reply(None, "nevermind",
                             replace_id=replace_id)

    def _handle_council_room_join(self, member, **kwargs):
        pass

    def _handle_council_room_exit(self, *, muc_leave_mode=None, muc_actor=None,
                                  muc_reason=None, **kwargs):
        if muc_leave_mode == aioxmpp.muc.LeaveMode.DISCONNECTED:
            # ignore this, because we’ll reconnect and re-join in
            # _stream_established
            return

        self.on_fatal_error(RuntimeError(
            "exited the MUC room unexpectedly: "
            "leave_mode={} actor={} reason={!r}".format(
                muc_leave_mode,
                muc_actor,
                muc_reason,
            )
        ))

    def _format_vote_summary(self, votes, past_tense):
        result = []

        yet_suffix = " (yet)" if past_tense else ""

        for actor, vote_info in votes.items():
            member_info = self._state.get_member_info(actor)

            if vote_info is None:
                result.append("{} has not voted{}".format(
                    member_info["nick"],
                    yet_suffix
                ))
                continue

            result.append(
                "{} has voted {}{}".format(
                    member_info["nick"],
                    vote_info["value"].value,
                    ": {}".format(vote_info["remark"])
                    if vote_info["remark"] else
                    " without further comment"
                )
            )

        return result

    def _handle_poll_concluded(self, poll_id, reason):
        metadata, votes = self._state.get_poll_info(poll_id)
        summary = self._state.get_vote_summary(poll_id)
        state = self._state.get_poll_state(poll_id)

        message = [
            "Poll {} concluded due to {}. It has {}{}.".format(
                metadata["topic"],
                reason.value,
                "passed" if summary["result"].has_passed else "failed",
                " (with veto)" if summary["result"].has_veto else "",
            )
        ]

        message.extend(self._format_vote_summary(summary["votes"], True))

        self._send_reply(
            None,
            "\n".join(message),
        )

    def _action_nothing(self, *args, **kwargs):
        return None, "as if it never happened"

    def _action_help(self, *args, **kwargs):
        return None, "https://github.com/horazont/councilbot/blob/master/docs/patterns.rst"

    def _action_create_poll(self, actor, message_id, remaining_words, params):
        try:
            tid, poll_id = self._state.create_poll(
                actor,
                message_id,
                " ".join(remaining_words),
            )
        except FileExistsError:
            return (
                None,
                "sorry, this is too close to the topic of another open poll. "
                "Please choose a new topic description."
            )

        vote_info, _ = self._state.get_poll_info(poll_id)

        return tid, "created poll on {}. Expires on {:%Y-%m-%d}".format(
            vote_info["topic"],
            vote_info["end_time"],
        )

    def _action_list_votes(self, actor, message_id, remaining_words, params):
        try:
            poll_id = self._state.find_poll(" ".join(remaining_words))
        except KeyError:
            return (
                None,
                "sorry, I do not know which poll you’re referring to"
            )

        metadata, _ = self._state.get_poll_info(poll_id)
        summary = self._state.get_vote_summary(poll_id)
        state = self._state.get_poll_state(poll_id)

        result = []
        result.append("poll on {} is {}. The poll {} {}{}{}.".format(
            metadata["topic"],
            state.value,
            "is" if state.is_open else "has",
            "pass" if summary["result"].has_passed else "fail",
            "ing" if state.is_open else "ed",
            " (with veto)" if summary["result"].has_veto else ""
        ))

        result.extend(self._format_vote_summary(
            summary["votes"],
            not state.is_open,
        ))

        return None, "\n".join(result)

    def _action_conclude_poll(self, actor, message_id, remaining_words, params):
        pass

    def _action_cast_vote(self, actor, message_id, remaining_words, params):
        value = councilbot.state.VoteValue(params["vote"])

        text = " ".join(remaining_words)
        poll_identifier, _, remark = text.partition(":")
        poll_identifier = poll_identifier.strip()
        remark = remark.strip()

        try:
            poll_id = self._state.find_poll(poll_identifier)
        except KeyError:
            return (
                None,
                "sorry, I do not know which vote you mean."
            )

        if value == councilbot.state.VoteValue.VETO and len(remark) < 10:
            return (
                None,
                "you have to give a reason when you veto. Tell me like this: "
                "'I vote -1 on xyz: because it has ugly ears' (the colon "
                "separates the poll topic and your reason)."
            )

        metadata, _ = self._state.get_poll_info(poll_id)

        try:
            tid = self._state.cast_vote(
                actor, message_id, poll_id, value, remark,
            )
        except Exception:
            self.logger.warning("failed to cast vote on poll %s",
                                poll_id, exc_info=True)
            return (
                None,
                "sorry, something went wrong while casting the vote :("
            )

        return tid, "I recorded your vote of {} on {}: {}".format(
            value.value,
            metadata["topic"],
            remark or "(no comment)",
        )

    def _action_delete_poll(self, actor, message_id, remaining_words, params):
        try:
            poll_id = self._state.find_poll(" ".join(remaining_words))
        except KeyError:
            return (
                None,
                "sorry, I do not know which poll you’re referring to"
            )

        metadata, _ = self._state.get_poll_info(poll_id)

        try:
            tid = self._state.delete_poll(actor, message_id, poll_id)
        except Exception:
            self.logger.warning("failed to delete poll %s",
                                poll_id, exc_info=True)
            return (
                None,
                "sorry, something went wrong while deleting the poll :("
            )

        return tid, "deleted poll on {}".format(metadata["topic"])

    def _action_list_polls(self, actor, message_id, remaining_words, params):
        if remaining_words:
            return (
                None,
                "I am not sure what you want "
                "(what is {!r} supposed to mean?).".format(
                    " ".join(remaining_words)
                )
            )

        now = datetime.utcnow()

        result = []

        polls = [self._state.get_poll_info(poll_id)
                 for poll_id in self._state.active_polls]

        for metadata, _ in sorted(polls,
                                  key=lambda x: x[0]["end_time"],
                                  reverse=True):
            result.append(
                "{} (due {}, on {:%Y-%m-%d})".format(
                    metadata["topic"],
                    babel.dates.format_timedelta(
                        (metadata["end_time"] - now),
                        locale="en_GB",
                        add_direction=True,
                        granularity="day",
                    ),
                    metadata["end_time"]
                )
            )

        if not result:
            result.append("there are currently no open polls")
        else:
            result.insert(0, "there {} {} open poll{}".format(
                "is" if len(polls) == 1 else "are",
                len(polls),
                "s" if len(polls) != 1 else ""
            ))

        return None, "\n".join(result)

    def _action_autoconclude(self, actor, message_id, remaining_words, params):
        if remaining_words:
            return (
                None,
                "I am not sure what you want "
                "(what is {!r} supposed to mean?).".format(
                    " ".join(remaining_words)
                )
            )

        if not self._state.autoconclude_polls(cutoff=timedelta(minutes=5)):
            return (
                None,
                "there are no open polls which qualify for conclusion at the "
                "moment"
            )

        return None, None
