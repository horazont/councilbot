Councilbot
##########

Councilbot is a text interface to committee meeting and poll management, written
for the XMPP Council.

Text Interface
==============

The text interface comes in two flavors: "short" and "natural language". Both
interfaces can be used interchangably and at the same time by different or even
the same members.

The *short* interface uses the familiar "!" style of commands (for example,
``!list``). There is no need to explicitly mention the bot for the short
interface.

The *natural language* interfae uses a more fancy style. To use it, the message
must be prefixed with the bots nickname followed by either a colon or a comma,
followed by a space (most clients will do that for you if you tap the bot
or use tab completion).

**Notation:** In the following examples, we will assume that the bot uses the
nickname ``Secretary``. Also, stuff which is in pointy brackets (``<>``) is
expected to be filled in by you. You must not type the pointy brackets.

General Notes
-------------

When operating on existing polls, the *subject* is always fuzzy matched. The
fuzzy match consists of two stages: first, an attempt to match against the *tag*
of any poll is made. This match has to be relatively high in confidence to work.
If this does not work, a fuzzy match against the entire poll subject is made.
This match needs lower confidence to let you leave out filler words when
referring to the poll.

When multiple polls match, you get one randomly. This is not a critical issue,
because all actions (including deletion) can be undone or modified with Last
Message Correction.

As mentioned, you can use Last Message Correction to amend your activities. LMC
is integrated like this: When you correct a message which triggered an action
in councilbot, that action is reversed. If the corrected message also triggers
an action, that action is executed afterwards. If councilbot replied to you
in response to your first message, it will also (attempt to) correct its reply
to match the new action (if any).

Short Interface
---------------

* ``!create <subject>``: Create a new poll on *subject*. If the *subject*
  contains a part in square brackets, that part is used as a *tag* of the poll.
  See the general notes section about how to use tags.

  Examples::

      !create Deprecate XEP-0001
      !create Accept "Cryptographic Hash Function Recommendations for XMPP" as Experimental XEP [hash-recommendations]

  The latter uses "hash-recommendations" as *tag* for the poll.

* ``!delete <subject>``: Delete the poll on *subject*.
* ``!list``: List all currently open polls.
* ``!show <subject>``: Show details on the poll on *subject*.
* ``!+1 <subject>``: Vote +1 on the poll on *subject*.
* ``!+1 <subject>: <remark>``: Vote +1 on the poll on *subject* while adding
  a *remark* to your vote.
* ``!+0``, ``!-0`` and ``!-1`` work exactly like ``!+1``, except that ``!-1``
  will force you to give at least a few characters of remark, because it’s
  required.

**Note:** You cannot use ``:`` in the ``<subject>`` for voting commands, because
``:`` is used to separate the subject and the remark. If you need to match a
subject which has a colon, either use the *tag* of the poll (if any) or simply
omit the ``:``; the fuzzy match will save you.

Natural Language Interface
--------------------------

In this section, stuff which is in square brackets are optional parts of
phrases which will be ignored by the bot. The ignoring is more flexible than
the examples though.

* ``Secretary, [please|I want to] create [a] poll [on] <subject>``: Same as
  ``!create <subject>``.
* ``Secretary, [please] list [all] open polls``: Same as ``!list``
* ``Secretary, [I want to|I] vote +1 [on] <subject>``: Same as ``!+1 <subject>``
* ``Secretary, [I want to|I] vote +1 [on] <subject>: <remark>``: Same as ``!+1 <subject>: <remark>``
* The analogous ``+0``, ``-0`` and ``-1`` commands exist.
* ``Secretary, [please|I want to] delete [the] poll [on] <subject>``: Same as ``!delete``
* ``Secretary, [please] show [the] votes [on] <subject>``: Same as ``!show <subject>``
