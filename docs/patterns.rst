This document lists the most basic syntax. The bot will ignore some filler
words and knows some aliases of words, allowing you to use more natural lanugage
with it.

When the examples show stuff in brackets, do not put the brackets in your actual
message. So if the example shows  ``foo <the topic>``, and ``fruit``  would be
the topic, you’d write ``foo fruit`` to the bot.

It only listens if you prefix the message with its nickname followed by either
a comma or a colon, followed by at least one space. (Though it will answer to
anyone who writes ``ping``.)

* Create a poll: ``Secretary, create poll <the subject>``
* List open polls: ``Secretary, list open polls``
* Cast a vote: ``Secretary: vote +1 on <the subject>: <reason>`` (the ``: <reason>`` part is optional, unless you try to veto)
* List all votes on a poll: ``Secretary, list votes on <the subject>``
* Delete a poll: ``Secretary, delete poll <the subject>``

You can use Last Message Correction for most actions. Using it with ``create``
is dangerous at the moment since it will delete and re-create the poll, losing
all votes which are already cast.

If after applying the Message Correction, the bot would not listen to you, it
simply reverts the action without executing another.

Not implemented yet, but nice to have:

* Reminders to people who have not voted yet at certain intervals before the
  poll expires
* Ability to show information about recently concluded/expired polls
* Ability to attach extra information to polls, e.g. github issues and editor
  actions
* Integration with github s.t. poll results are automatically commented onto
  the linked PRs/Issues

Examples of more verbose commands:

* ``Secretary, I want to create a poll on <the subject>``
* ``Secretary, I vote +1 on <the subject>``
* ``Secretary, please list all open polls``
