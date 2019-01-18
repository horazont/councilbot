import collections
import re

import lxml

import aiohttp


PROTOXEP_URL_RE = re.compile(
    r"https?://(www\.)?xmpp\.org/extensions/inbox/(?P<basename>.+)\.(html|xml)",
    re.I,
)
PROTOXEP_URL_TEMPLATE = "https://xmpp.org/extensions/inbox/{basename}.xml"
PROTOXEP_HTML_URL_TEMPLATE = "https://xmpp.org/extensions/inbox/{basename}.html"

XEPS_PR_URL = re.compile(
    r"https?://(www\.)?github\.com/xsf/xeps/pull/(?P<num>[0-9]+)\S*",
    re.I,
)
XEPS_PR_URL_TEMPLATE = "https://api.github.com/repos/xsf/xeps/pulls/{num}"
XEPS_PR_FILES_URL_TEMPLATE = \
    "https://api.github.com/repos/xsf/xeps/pulls/{num}/files"
MAX_READ_SIZE = 10*1024*1024  # 10 MiB -- more than enough for all current XEPs
READ_CHUNK_SIZE = 4096

BAD_SHORT_NAME_RE = re.compile(
    "^not[\W_]yet[\W_]assigned|None|N/A$",
    re.I,
)


URLMetadata = collections.namedtuple(
    "URLMetadata",
    [
        "matched_url",
        "title",
        "description",
        "urls",
        "tag",
    ]
)


async def _feed_read(source, sink, max_size):
    nread = 0
    while nread < max_size:
        blob = await source(READ_CHUNK_SIZE)
        if not blob:
            break
        sink(blob)
        nread += len(blob)


async def _extract_protoxep_metadata(match):
    basename = match.groupdict()["basename"]
    url = PROTOXEP_URL_TEMPLATE.format(basename=basename)

    parser = lxml.etree.XMLParser(resolve_entities=False)

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            await _feed_read(response.content.read, parser.feed, MAX_READ_SIZE)

    tree = parser.close()

    title_el, = tree.xpath("/xep/header/title")
    abstract_el, = tree.xpath("/xep/header/abstract")
    short_name_el, = tree.xpath("/xep/header/shortname")

    short_name = short_name_el.text
    if BAD_SHORT_NAME_RE.search(short_name):
        short_name = basename

    return URLMetadata(
        matched_url=match.group(0),
        title="Accept {!r} as Experimental".format(title_el.text),
        description=abstract_el.text,
        tag=short_name,
        urls=[
            PROTOXEP_HTML_URL_TEMPLATE.format(basename=basename),
        ]
    )


_IMPLEMENTATIONS = [
    (PROTOXEP_URL_RE, _extract_protoxep_metadata)
]


async def extract_url_metadata(url):
    for match_re, extractor in _IMPLEMENTATIONS:
        match = match_re.search(url)
        if match is None:
            continue

        return (await _extract_protoxep_metadata(match))

    return None
