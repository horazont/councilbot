import contextlib
import copy
import io
import itertools
import unittest
import unittest.mock

from datetime import datetime, timedelta

import aioxmpp

import councilbot.state as state


class TestPoll(unittest.TestCase):
    def setUp(self):
        self.id_ = "some_id"
        self.start = datetime(2019, 1, 1, 10, 0, 0)
        self.members = [
            aioxmpp.JID.fromstr("alice@domain.example"),
            aioxmpp.JID.fromstr("bob@domain.example"),
            aioxmpp.JID.fromstr("carol@domain.example"),
            aioxmpp.JID.fromstr("romeo@domain.example"),
            aioxmpp.JID.fromstr("juliet@domain.example"),
        ]
        self.subject = "accept foo"
        self.p = state.Poll(
            self.id_,
            self.start,
            timedelta(days=14),
            self.subject,
            self.members,
        )

    def _make_dummy_votes(self, p):
        p.push_vote(
            self.members[0],
            state.VoteValue.MINUS_ZERO,
            None,
            timestamp=p.start_time + timedelta(days=1),
        )

        p.push_vote(
            self.members[1],
            state.VoteValue.ACK,
            None,
            timestamp=p.start_time + timedelta(days=2),
        )

        p.push_vote(
            self.members[0],
            state.VoteValue.ACK,
            "makes more sense than I thought",
            timestamp=p.start_time + timedelta(days=3),
        )

        p.push_vote(
            self.members[2],
            state.VoteValue.VETO,
            "duplicates XEP-0001",
            timestamp=p.start_time + timedelta(days=4),
        )

        p.push_vote(
            self.members[3],
            state.VoteValue.ACK,
            None,
            timestamp=p.start_time + timedelta(days=5),
        )

        p.push_vote(
            self.members[4],
            state.VoteValue.PLUS_ZERO,
            None,
            timestamp=p.start_time + timedelta(days=6),
        )

    def test_attributes(self):
        self.assertEqual(self.p.id_, self.id_)
        self.assertEqual(self.p.start_time, self.start)
        self.assertEqual(self.p.end_time, self.start + timedelta(days=14))
        self.assertEqual(self.p.result, state.PollResult.FAIL)
        self.assertSetEqual(self.p.flags, set())

    def test_get_state_returns_open_while_no_votes_and_before_end_time(self):
        for d in range(14):
            self.assertEqual(
                self.p.get_state(self.start + timedelta(days=d)),
                state.PollState.OPEN,
            )

    def test_get_state_returns_expired_on_poll_without_votes_after_end_time(self):  # NOQA
        self.assertEqual(
            self.p.get_state(self.start + timedelta(days=14)),
            state.PollState.EXPIRED,
        )

    def test_get_votes_returns_empty_list(self):
        self.assertEqual(
            self.p.get_votes(self.members[0]),
            [],
        )

    def test_get_votes_fails_for_non_member(self):
        with self.assertRaises(KeyError):
            self.p.get_votes(unittest.mock.sentinel.non_member)

    def test_get_vote_history_returns_mapping_with_empty_lists(self):
        result = self.p.get_vote_history()

        self.assertDictEqual(
            result,
            {
                member: []
                for member in self.members
            }
        )

    def test_get_current_votes_returns_member_mapping(self):
        self.assertDictEqual(
            self.p.get_current_votes(),
            {
                member: None
                for member in self.members
            }
        )

    def test_push_vote_fails_for_non_member(self):
        with self.assertRaises(KeyError):
            self.p.push_vote(unittest.mock.sentinel.non_member,
                             unittest.mock.sentinel.value,
                             unittest.mock.sentinel.remark)

        self.assertDictEqual(
            self.p.get_vote_history(),
            {
                member: []
                for member in self.members
            }
        )

        self.assertDictEqual(
            self.p.get_current_votes(),
            {
                member: None
                for member in self.members
            }
        )

    def test_push_vote_makes_vote_appear_in_getters(self):
        with contextlib.ExitStack() as stack:
            VoteRecord = stack.enter_context(unittest.mock.patch(
                "councilbot.state.VoteRecord",
            ))
            VoteRecord.return_value = unittest.mock.sentinel.vote_record

            self.p.push_vote(self.members[0],
                             unittest.mock.sentinel.value,
                             unittest.mock.sentinel.remark,
                             timestamp=unittest.mock.sentinel.timestamp)

        VoteRecord.assert_called_once_with(
            unittest.mock.sentinel.timestamp,
            unittest.mock.sentinel.value,
            unittest.mock.sentinel.remark,
        )

        self.assertEqual(
            self.p.get_votes(self.members[0]),
            [
                unittest.mock.sentinel.vote_record,
            ]
        )

        self.assertDictEqual(
            self.p.get_vote_history(),
            {
                self.members[0]: [
                    unittest.mock.sentinel.vote_record,
                ],
                self.members[1]: [],
                self.members[2]: [],
                self.members[3]: [],
                self.members[4]: [],
            }
        )

        self.assertDictEqual(
            self.p.get_current_votes(),
            {
                self.members[0]:
                    unittest.mock.sentinel.vote_record,
                self.members[1]: None,
                self.members[2]: None,
                self.members[3]: None,
                self.members[4]: None,
            }
        )

    def test_push_vote_stacks_in_history(self):
        def record_gen():
            for i in itertools.count():
                yield getattr(unittest.mock.sentinel, "record{}".format(i))

        with contextlib.ExitStack() as stack:
            VoteRecord = stack.enter_context(unittest.mock.patch(
                "councilbot.state.VoteRecord",
            ))
            VoteRecord.side_effect = record_gen()

            self.p.push_vote(self.members[0],
                             unittest.mock.sentinel.value,
                             unittest.mock.sentinel.remark,
                             timestamp=unittest.mock.sentinel.timestamp)

            self.p.push_vote(self.members[0],
                             unittest.mock.sentinel.value,
                             unittest.mock.sentinel.remark,
                             timestamp=unittest.mock.sentinel.timestamp)

        self.assertEqual(
            self.p.get_votes(self.members[0]),
            [
                unittest.mock.sentinel.record0,
                unittest.mock.sentinel.record1,
            ]
        )

        self.assertDictEqual(
            self.p.get_vote_history(),
            {
                self.members[0]: [
                    unittest.mock.sentinel.record0,
                    unittest.mock.sentinel.record1,
                ],
                self.members[1]: [],
                self.members[2]: [],
                self.members[3]: [],
                self.members[4]: [],
            }
        )

    def test_push_vote_updates_current(self):
        def record_gen():
            for i in itertools.count():
                yield getattr(unittest.mock.sentinel, "record{}".format(i))

        with contextlib.ExitStack() as stack:
            VoteRecord = stack.enter_context(unittest.mock.patch(
                "councilbot.state.VoteRecord",
            ))
            VoteRecord.side_effect = record_gen()

            self.p.push_vote(self.members[0],
                             unittest.mock.sentinel.value,
                             unittest.mock.sentinel.remark,
                             timestamp=unittest.mock.sentinel.timestamp)

            self.p.push_vote(self.members[0],
                             unittest.mock.sentinel.value,
                             unittest.mock.sentinel.remark,
                             timestamp=unittest.mock.sentinel.timestamp)

        self.assertDictEqual(
            self.p.get_current_votes(),
            {
                self.members[0]:
                    unittest.mock.sentinel.record1,
                self.members[1]: None,
                self.members[2]: None,
                self.members[3]: None,
                self.members[4]: None,
            }
        )

    def test_push_vote_for_different_members(self):
        def record_gen():
            for i in itertools.count():
                yield getattr(unittest.mock.sentinel, "record{}".format(i))

        with contextlib.ExitStack() as stack:
            VoteRecord = stack.enter_context(unittest.mock.patch(
                "councilbot.state.VoteRecord",
            ))
            VoteRecord.side_effect = record_gen()

            self.p.push_vote(self.members[0],
                             unittest.mock.sentinel.value,
                             unittest.mock.sentinel.remark,
                             timestamp=unittest.mock.sentinel.timestamp)

            self.p.push_vote(self.members[1],
                             unittest.mock.sentinel.value,
                             unittest.mock.sentinel.remark,
                             timestamp=unittest.mock.sentinel.timestamp)

            self.p.push_vote(self.members[0],
                             unittest.mock.sentinel.value,
                             unittest.mock.sentinel.remark,
                             timestamp=unittest.mock.sentinel.timestamp)

        self.assertEqual(
            self.p.get_votes(self.members[0]),
            [
                unittest.mock.sentinel.record0,
                unittest.mock.sentinel.record2,
            ]
        )

        self.assertEqual(
            self.p.get_votes(self.members[1]),
            [
                unittest.mock.sentinel.record1,
            ]
        )

        self.assertDictEqual(
            self.p.get_vote_history(),
            {
                self.members[0]: [
                    unittest.mock.sentinel.record0,
                    unittest.mock.sentinel.record2,
                ],
                self.members[1]: [
                    unittest.mock.sentinel.record1,
                ],
                self.members[2]: [],
                self.members[3]: [],
                self.members[4]: [],
            }
        )

        self.assertDictEqual(
            self.p.get_current_votes(),
            {
                self.members[0]:
                    unittest.mock.sentinel.record2,
                self.members[1]:
                    unittest.mock.sentinel.record1,
                self.members[2]: None,
                self.members[3]: None,
                self.members[4]: None,
            }
        )

    def test_pop_vote_reverts_push_vote(self):
        self.p.push_vote(self.members[0],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.p.pop_vote(self.members[0])

        self.assertSequenceEqual(
            self.p.get_votes(self.members[0]),
            [],
        )

        self.assertDictEqual(
            self.p.get_vote_history(),
            {
                member: []
                for member in self.members
            }
        )

        self.assertDictEqual(
            self.p.get_current_votes(),
            {
                member: None
                for member in self.members
            }
        )

    def test_pop_vote_does_nothing_on_no_existing_votes(self):
        self.p.pop_vote(self.members[0])

    def test_get_state_returns_complete_if_all_members_have_voted_and_not_expired_yet(self):  # NOQA
        self.p.push_vote(self.members[0],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.p.push_vote(self.members[1],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.p.push_vote(self.members[2],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.p.push_vote(self.members[3],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.p.push_vote(self.members[4],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.assertEqual(
            self.p.get_state(self.start),
            state.PollState.COMPLETE,
        )

    def test_get_state_returns_concluded_if_all_members_have_voted_and_poll_expired(self):  # NOQA
        self.p.push_vote(self.members[0],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.p.push_vote(self.members[1],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.p.push_vote(self.members[2],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.p.push_vote(self.members[3],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.p.push_vote(self.members[4],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)

        self.assertEqual(
            self.p.get_state(self.start + timedelta(days=14)),
            state.PollState.CONCLUDED,
        )

    def test_dump_writes_toml(self):
        with contextlib.ExitStack() as stack:
            dump = stack.enter_context(unittest.mock.patch("toml.dump"))

            self.p.dump(unittest.mock.sentinel.f)

        dump.assert_called_once_with(unittest.mock.ANY,
                                     unittest.mock.sentinel.f)

        _, (data, _), _ = dump.mock_calls[-1]

        self.assertDictEqual(
            data,
            {
                "id": self.id_,
                "start_time": self.start,
                "end_time": self.start + timedelta(days=14),
                "subject": self.subject,
                "flags": [],
                "votes": {
                    str(self.members[0]): [],
                    str(self.members[1]): [],
                    str(self.members[2]): [],
                    str(self.members[3]): [],
                    str(self.members[4]): [],
                }
            }
        )

    def test_dump_serialises_votes_properly(self):
        self._make_dummy_votes(self.p)

        with contextlib.ExitStack() as stack:
            dump = stack.enter_context(unittest.mock.patch("toml.dump"))

            self.p.dump(unittest.mock.sentinel.f)

        dump.assert_called_once_with(unittest.mock.ANY,
                                     unittest.mock.sentinel.f)

        _, (data, _), _ = dump.mock_calls[-1]

        self.assertDictEqual(
            data,
            {
                "id": self.id_,
                "start_time": self.start,
                "end_time": self.start + timedelta(days=14),
                "subject": self.subject,
                "flags": [],
                "votes": {
                    str(self.members[0]): [
                        {
                            "timestamp": self.start + timedelta(days=1),
                            "value": "-0",
                            "remark": None,
                        },
                        {
                            "timestamp": self.start + timedelta(days=3),
                            "value": "+1",
                            "remark": "makes more sense than I thought",
                        },
                    ],
                    str(self.members[1]): [
                        {
                            "timestamp": self.start + timedelta(days=2),
                            "value": "+1",
                            "remark": None,
                        }
                    ],
                    str(self.members[2]): [
                        {
                            "timestamp": self.start + timedelta(days=4),
                            "value": "-1",
                            "remark": "duplicates XEP-0001",
                        }
                    ],
                    str(self.members[3]): [
                        {
                            "timestamp": self.start + timedelta(days=5),
                            "value": "+1",
                            "remark": None,
                        },
                    ],
                    str(self.members[4]): [
                        {
                            "timestamp": self.start + timedelta(days=6),
                            "value": "+0",
                            "remark": None,
                        },
                    ],
                }
            }
        )

    def test_dump_generates_valid_toml(self):
        self._make_dummy_votes(self.p)

        out = io.StringIO()

        self.p.dump(out)

    def test_load_restores_from_dump(self):
        buf = io.StringIO()
        self._make_dummy_votes(self.p)
        self.p.flags.add(state.PollFlag.CONCLUDED)
        self.p.dump(buf)
        buf.seek(0, io.SEEK_SET)
        p2 = state.Poll.load(buf)

        self.assertEqual(self.p.id_, p2.id_)
        self.assertEqual(self.p.start_time, p2.start_time)
        self.assertEqual(self.p.end_time, p2.end_time)
        self.assertEqual(self.p.result, p2.result)
        self.assertEqual(self.p.subject, p2.subject)
        self.assertSetEqual(self.p.flags, p2.flags)
        self.assertDictEqual(
            self.p.get_vote_history(),
            p2.get_vote_history()
        )

    def test_flags_can_be_modified(self):
        self.p.flags.add(state.PollFlag.CONCLUDED)
        self.assertSetEqual(
            self.p.flags,
            {state.PollFlag.CONCLUDED}
        )

    def test_copy_has_independent_member_data_1(self):
        p2 = copy.copy(self.p)
        self.p.push_vote(self.members[0],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)
        p2.push_vote(self.members[1],
                     unittest.mock.sentinel.value,
                     unittest.mock.sentinel.remark)

        self.assertNotEqual(self.p.get_vote_history(),
                            p2.get_vote_history())

    def test_copy_has_independent_member_data_2(self):
        self.p.push_vote(self.members[0],
                         unittest.mock.sentinel.value,
                         unittest.mock.sentinel.remark)
        p2 = copy.copy(self.p)
        p2.push_vote(self.members[0],
                     unittest.mock.sentinel.value,
                     unittest.mock.sentinel.remark)

        self.assertNotEqual(self.p.get_vote_history(),
                            p2.get_vote_history())

    def test_copy_has_independent_flags(self):
        self.p.flags.add(state.PollFlag.CONCLUDED)
        p2 = copy.copy(self.p)
        p2.flags.clear()

        self.assertNotEqual(self.p.flags, p2.flags)

    def test_copy_produces_copy(self):
        self._make_dummy_votes(self.p)
        self.p.flags.add(state.PollFlag.CONCLUDED)
        p2 = copy.copy(self.p)

        self.assertEqual(self.p.id_, p2.id_)
        self.assertEqual(self.p.start_time, p2.start_time)
        self.assertEqual(self.p.end_time, p2.end_time)
        self.assertEqual(self.p.result, p2.result)
        self.assertEqual(self.p.subject, p2.subject)
        self.assertSetEqual(self.p.flags, p2.flags)
        self.assertDictEqual(
            self.p.get_vote_history(),
            p2.get_vote_history()
        )
