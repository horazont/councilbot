import argparse
import asyncio
import functools
import logging
import math
import pathlib
import re
import signal

from datetime import datetime, timedelta

import babel.dates

import toml

import aioxmpp
import aioxmpp.muc.xso
import aioxmpp.xso

from . import state, bot


logger = logging.getLogger("main")


async def amain(loop, args, config):
    context = state.State(config)

    client = aioxmpp.Client(
        config["xmpp"]["address"],
        aioxmpp.make_security_layer(config["xmpp"]["password"]),
        logger=logger.getChild("client")
    )

    disco_srv = client.summon(aioxmpp.DiscoServer)
    council_bot = client.summon(bot.CouncilBot)
    council_bot.set_state_object(context)
    council_bot.set_room(config["council"]["room"],
                         config["council"]["nick"])
    fatal_error = council_bot.on_fatal_error.future()

    disco_srv.set_identity_names(
        "client", "bot",
        names={
            aioxmpp.structs.LanguageTag.fromstr("en"): "Council Bot"
        }
    )

    stop_signal = asyncio.Event()
    loop.add_signal_handler(signal.SIGINT, stop_signal.set)
    loop.add_signal_handler(signal.SIGTERM, stop_signal.set)

    futures = [
        asyncio.ensure_future(stop_signal.wait()),
        fatal_error,
    ]

    async with client.connected() as stream:
        done, pending = await asyncio.wait(
            futures,
            return_when=asyncio.FIRST_COMPLETED
        )

        if fatal_error in done:
            try:
                fatal_error.result()
            except BaseException as exc:
                logger.error("council bot crashed", exc_info=True)
                return

        logger.info("received SIGINT/SIGTERM, initiating clean shutdown")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c", "--config",
        required=True,
        type=pathlib.Path,
    )
    parser.add_argument(
        "-v",
        dest="verbosity",
        action="count",
        default=0,
        help="Increase verbosity (up to -vvv)"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level={
            0: logging.ERROR,
            1: logging.WARNING,
            2: logging.INFO,
        }.get(args.verbosity, logging.DEBUG)
    )
    logging.getLogger("aioxmpp").setLevel(logging.INFO)
    logging.getLogger("aioopenssl").setLevel(logging.WARNING)

    with args.config.open("r") as f:
        cfg = toml.load(f)

    cfg["xmpp"]["address"] = aioxmpp.JID.fromstr(cfg["xmpp"]["address"])

    cfg["council"]["room"] = aioxmpp.JID.fromstr(cfg["council"]["room"])

    for member in cfg["council"]["members"]:
        member["address"] = aioxmpp.JID.fromstr(member["address"])

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(amain(loop, args, cfg))
    finally:
        loop.close()
