import contextlib
import copy
import io
import itertools
import unittest
import unittest.mock

from datetime import datetime, timedelta

import toml

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
        self.assertEqual(self.p.tag, None)
        self.assertSequenceEqual(self.p.urls, [])
        self.assertIsNone(self.p.description)

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
                "tag": None,
                "urls": [],
                "votes": {
                    str(self.members[0]): [],
                    str(self.members[1]): [],
                    str(self.members[2]): [],
                    str(self.members[3]): [],
                    str(self.members[4]): [],
                }
            }
        )

    def test_dump_serialises_metadata(self):
        self.p.tag = "foo"
        self.p.flags.add(state.PollFlag.CONCLUDED)
        self.p.urls.append("https://domain.example/foo")
        self.p.description = "transfnordistan express"

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
                "flags": ["concluded"],
                "tag": "foo",
                "description": "transfnordistan express",
                "urls": [
                    "https://domain.example/foo"
                ],
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
                "tag": None,
                "urls": [],
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
        self.p.tag = "fnord"
        self.p.urls.append("https://domain.example/foo")
        self.p.description = "foobar"
        self.p.dump(buf)
        buf.seek(0, io.SEEK_SET)
        p2 = state.Poll.load(buf)

        self.assertEqual(self.p.id_, p2.id_)
        self.assertEqual(self.p.start_time, p2.start_time)
        self.assertEqual(self.p.end_time, p2.end_time)
        self.assertEqual(self.p.result, p2.result)
        self.assertEqual(self.p.subject, p2.subject)
        self.assertSetEqual(self.p.flags, p2.flags)
        self.assertEqual(self.p.tag, p2.tag)
        self.assertDictEqual(
            self.p.get_vote_history(),
            p2.get_vote_history()
        )
        self.assertSequenceEqual(
            self.p.urls,
            p2.urls,
        )
        self.assertEqual(self.p.description, p2.description)

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

    def test_copy_has_independent_urls(self):
        p2 = copy.copy(self.p)
        self.assertIsNot(self.p.urls, p2.urls)

    def test_copy_produces_copy(self):
        self._make_dummy_votes(self.p)
        self.p.flags.add(state.PollFlag.CONCLUDED)
        self.p.tag = "foo"
        self.p.urls.append("bar")
        self.p.description = "fnord"
        p2 = copy.copy(self.p)

        self.assertEqual(self.p.id_, p2.id_)
        self.assertEqual(self.p.start_time, p2.start_time)
        self.assertEqual(self.p.end_time, p2.end_time)
        self.assertEqual(self.p.result, p2.result)
        self.assertEqual(self.p.subject, p2.subject)
        self.assertSetEqual(self.p.flags, p2.flags)
        self.assertEqual(self.p.tag, p2.tag)
        self.assertSequenceEqual(self.p.urls, p2.urls)
        self.assertDictEqual(
            self.p.get_vote_history(),
            p2.get_vote_history()
        )
        self.assertEqual(self.p.description, p2.description)

    def test_poll_is_passing_with_all_acks(self):
        for member in self.members:
            self.p.push_vote(member, state.VoteValue.ACK, None)

        self.assertEqual(self.p.result, state.PollResult.PASS)

    def test_poll_is_passing_with_majority_acks(self):
        for member in self.members[:3]:
            self.p.push_vote(member, state.VoteValue.ACK, None)
        for member in self.members[3:]:
            self.p.push_vote(member, state.VoteValue.MINUS_ZERO, None)

        self.assertEqual(self.p.result, state.PollResult.PASS)

    def test_poll_is_veto_with_single_veto(self):
        for member in self.members[:4]:
            self.p.push_vote(member, state.VoteValue.ACK, None)
        for member in self.members[4:]:
            self.p.push_vote(member, state.VoteValue.VETO, None)

        self.assertEqual(self.p.result, state.PollResult.VETO)

    def test_poll_is_failing_without_majority_of_votes(self):
        for member in self.members[:2]:
            self.p.push_vote(member, state.VoteValue.ACK, None)

        self.assertEqual(self.p.result, state.PollResult.FAIL)

    def test_poll_is_passing_with_majority_of_quorum_acks(self):
        for member in self.members[:2]:
            self.p.push_vote(member, state.VoteValue.ACK, None)
        for member in self.members[2:3]:
            self.p.push_vote(member, state.VoteValue.MINUS_ZERO, None)

        self.assertEqual(self.p.result, state.PollResult.PASS)

    def test_poll_is_failing_without_majority_of_quorum_acks(self):
        for member in self.members[:2]:
            self.p.push_vote(member, state.VoteValue.ACK, None)
        for member in self.members[2:4]:
            self.p.push_vote(member, state.VoteValue.MINUS_ZERO, None)

        self.assertEqual(self.p.result, state.PollResult.FAIL)

    def test_can_load_first_stable_format(self):
        data = {
            "id": self.id_,
            "start_time": self.start,
            "end_time": self.start + timedelta(days=14),
            "subject": self.subject,
            "flags": {state.PollFlag.CONCLUDED.value},
            "tag": "some-tag",
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

        f = io.StringIO()
        toml.dump(data, f)
        f.seek(0, io.SEEK_SET)

        p = state.Poll.load(f)

        self.assertEqual(p.id_, self.id_)
        self.assertEqual(p.start_time, self.start)
        self.assertEqual(p.end_time, self.start + timedelta(days=14))
        self.assertEqual(p.subject, self.subject)
        self.assertSetEqual(p.flags, {state.PollFlag.CONCLUDED})
        self.assertEqual(p.tag, "some-tag")
        self.assertIsNone(p.description)

        self.maxDiff = None
        self.assertDictEqual(
            p.get_vote_history(),
            {
                self.members[0]: [
                    state.VoteRecord(
                        self.start + timedelta(days=1),
                        state.VoteValue.MINUS_ZERO,
                        None,
                    ),
                    state.VoteRecord(
                        self.start + timedelta(days=3),
                        state.VoteValue.ACK,
                        "makes more sense than I thought",
                    ),
                ],
                self.members[1]: [
                    state.VoteRecord(
                        self.start + timedelta(days=2),
                        state.VoteValue.ACK,
                        None,
                    ),
                ],
                self.members[2]: [
                    state.VoteRecord(
                        self.start + timedelta(days=4),
                        state.VoteValue.VETO,
                        "duplicates XEP-0001"
                    ),
                ],
                self.members[3]: [
                    state.VoteRecord(
                        self.start + timedelta(days=5),
                        state.VoteValue.ACK,
                        None,
                    ),
                ],
                self.members[4]: [
                    state.VoteRecord(
                        self.start + timedelta(days=6),
                        state.VoteValue.PLUS_ZERO,
                        None,
                    ),
                ],
            }
        )
