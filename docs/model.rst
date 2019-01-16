Data Model
##########

Requirements
============

* Fully human-readable file-system based
* Always persistent: votes must not get lost after acknowledged by the bot,
  barring hard drive failure and pulling the power in the wrong moment
* Must be able to fully recover vote state (including which announcements have
  been made) after restart

Model
=====

The model is a state directory wherein different subdirectories exist.

``votes/``
---------

* One directory per vote, therein:

    * ``metadata.toml``: Holds basic metadata of the vote

        .. code:: toml

            start_time =
            end_time =
            topic = "the actual topic text"
            actor = "address@of-council.member"
            dirname = "..."

    * ``concluded.flag``: Flag file whose presence indicates that the software
      has processed the conclusion of the vote.
    * ``deleted.flag``: Flag file to indicate that the vote has been deleted.
    * ``vote-{member_id}.toml``: Holds information about the vote history of
      a council member.

      Data structure is like this:

      .. code:: toml

        [[vote]]
        value = "+0"
        remark = "then again, I don’t care either way"

        [[vote]]
        value = "+1"
        remark = "This is a good idea"

``members/``
------------

* ``{member_id}.toml``:

    .. code:: toml

        [last_transaction]
        member_message_id="xyz"
        our_message_id="abc"
        action="vote"
        target="2019-01-11-some-vote-slug"

    The last_action info is used to revert actions when a LMC is performed.

Transaction Concept
===================

Transactions consist of:

* Transaction ID (used as message ID for the confirmation message)
* Actor
* Action
* Additional Data to restore the previous state on rollback

Only the last transaction of each council member is stored.

Actions:

* create/start vote
* rename vote
* delete/cancel vote
* (re-)cast vote

Correction matrix:

* create -> any => delete first, execute second
* create -> create => rename first to topic of second, keep votes
* delete -> any => restore first (how?), execute second
* rename -> any => undo rename (how?), execute second
* cast -> cast (on same vote) => update existing vote without recording a new history item
* cast -> any => remove vote from history, execute second
