import enum
import re


class Action(enum.Enum):
    CREATE_POLL = "create_poll"
    CONCLUDE_POLL = "conclude_poll"
    AUTO_CONCLUDE_OPEN_POLLS = "auto_conclude_open_polls"
    DELETE_POLL = "delete_poll"
    CAST_VOTE = "cast_vote"
    HELP = "help"
    LIST_POLLS = "list_polls"
    LIST_VOTES = "list_votes"
    LIST_GENERIC = "list_generic"
    THANK = "thank"
    NULL = None


class PollSelector(enum.Enum):
    OPEN = "open"
    CONCLUDED = "concluded"
    EXPIRED = "expired"


class TextNode:
    def __init__(self, match, *, action=None,
                 save=None, save_const=None,
                 children=[], skip=[]):
        super().__init__()
        if isinstance(match, str):
            match = re.compile(re.escape(match))
        self.match = match
        self.children = children
        self.skip = skip
        self.action = action
        self.save = save
        self.save_const = save_const

    def parse(self, words, params={}):
        remaining_words = list(words)
        while remaining_words and remaining_words[0].casefold() in self.skip:
            del remaining_words[0]

        if not remaining_words:
            return self, remaining_words, params

        first_word = remaining_words[0]
        for child in self.children:
            match = child.match.match(first_word)
            if match is None:
                continue

            groups = match.groupdict()

            if child.save is not None:
                params = params.copy()
                params[child.save] = (
                    child.save_const or groups.get("save") or first_word
                )

            to_push = groups.get("push")

            if to_push:
                remaining_words.insert(1, to_push)

            return child.parse(remaining_words[1:], params)

        if self.action is not None:
            return self, remaining_words, params

        return None

    def __repr__(self):
        return "<TextNode match={!r} action={!r}>".format(
            self.match,
            self.action,
        )


_VOTEWORDS_SKIP = ["on", "the"]


_POLL_LIST_NODE = TextNode(
    re.compile(r"(vote|poll|ballot)s?", re.I),
    action=Action.LIST_POLLS,
)


PARSE_TREE = TextNode(
    None,
    skip=["i", "want", "to", "please", "do", "can", "you"],
    children=[
        TextNode(
            re.compile(r"create|add|start", re.I),
            skip=["a", "new"],
            children=[
                TextNode(
                    re.compile(r"vote|ballot|poll", re.I),
                    skip=_VOTEWORDS_SKIP,
                    action=Action.CREATE_POLL
                ),
            ]
        ),
        TextNode(
            re.compile(r"delete|remove|cancel", re.I),
            skip=["the"],
            children=[
                TextNode(
                    re.compile(r"vote|ballot|poll", re.I),
                    skip=_VOTEWORDS_SKIP,
                    action=Action.DELETE_POLL
                ),
            ]
        ),
        TextNode(
            re.compile(r"conclude|close", re.I),
            skip=["the"],
            children=[
                TextNode(
                    re.compile(r"all", re.I),
                    skip=["the", "pending", "open", "outstanding"],
                    children=[
                        TextNode(
                            re.compile(r"votes?", re.I),
                            action=Action.AUTO_CONCLUDE_OPEN_POLLS,
                        )
                    ]
                ),
                TextNode(
                    re.compile(r"vote|ballot|poll", re.I),
                    skip=_VOTEWORDS_SKIP,
                    action=Action.CONCLUDE_POLL,
                )
            ]
        ),
        TextNode(
            re.compile(r"!(?P<save>[+-][01])(?P<push>:.+)?"),
            save="vote",
            skip=_VOTEWORDS_SKIP,
            action=Action.CAST_VOTE,
        ),
        TextNode(
            re.compile(r"vote", re.I),
            children=[
                TextNode(
                    re.compile(r"[+-][01]"),
                    save="vote",
                    skip=_VOTEWORDS_SKIP,
                    action=Action.CAST_VOTE,
                )
            ]
        ),
        TextNode(
            re.compile(r"!?help", re.I),
            action=Action.HELP,
        ),
        TextNode(
            re.compile(r"disregard|nevermind", re.I),
            action=Action.NULL,
        ),
        TextNode(
            re.compile(r"thanks?", re.I),
            action=Action.THANK,
        ),
        TextNode(
            re.compile("!list", re.I),
            action=Action.LIST_GENERIC,
        ),
        TextNode(
            re.compile(r"show|list", re.I),
            skip=["the", "all", "me"],
            children=[
                TextNode(
                    re.compile(r"outstanding|pending|open", re.I),
                    save="selector",
                    save_const=PollSelector.OPEN,
                    children=[_POLL_LIST_NODE]
                ),
                TextNode(
                    re.compile(r"closed|concluded", re.I),
                    save="selector",
                    save_const=PollSelector.CONCLUDED,
                    children=[_POLL_LIST_NODE]
                ),
                TextNode(
                    re.compile(r"expired", re.I),
                    save="selector",
                    save_const=PollSelector.EXPIRED,
                    children=[_POLL_LIST_NODE]
                ),
                TextNode(
                    re.compile(r"votes?", re.I),
                    skip=_VOTEWORDS_SKIP,
                    action=Action.LIST_VOTES,
                ),
                _POLL_LIST_NODE
            ]
        )
    ]
)
