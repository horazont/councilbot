import bs4
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

XEPS_PR_URL_RE = re.compile(
    r"https?://(www\.)?github\.com/xsf/xeps/pull/(?P<num>[0-9]+)\S*",
    re.I,
)
XEPS_PR_URL_TEMPLATE = "https://api.github.com/repos/xsf/xeps/pulls/{num}"
XEPS_PR_HTML_URL_TEMPLATE = "https://github.com/xsf/xeps/pull/{num}"
XEPS_PR_FILES_URL_TEMPLATE = \
    "https://api.github.com/repos/xsf/xeps/pulls/{num}/files"
MAX_READ_SIZE = 10*1024*1024  # 10 MiB -- more than enough for all current XEPs
READ_CHUNK_SIZE = 4096
XEP_FILE_RE = re.compile(r"(xep-[0-9]{4}).xml")

STANDARDS_URL_RE = re.compile(
    r"https://mail.jabber.org/pipermail/standards/.+\.html",
    re.I,
)

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


async def _extract_xeps_pr_metadata(match):
    matched_url = match.group(0)
    num = match.groupdict()["num"]
    url = XEPS_PR_URL_TEMPLATE.format(num=num)
    files_url = XEPS_PR_FILES_URL_TEMPLATE.format(num=num)

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            pr_json = await response.json()

        files_json = []
        # using the xep name in the tag makes it too long for fuzzy match (even
        # without PR prefix)
        # async with session.get(files_url) as response:
        #     files_json = await response.json()

    title = pr_json["title"]
    # normalize all the spacing
    description = " ".join(pr_json["body"].split())
    if len(description) > 300:
        description = description[:300] + "…"

    tag = "PR#{}".format(num)

    affected_xep = None
    for file_info in files_json:
        match = XEP_FILE_RE.match(file_info["filename"])
        if match is None:
            continue
        if affected_xep:
            affected_xep = None
            break

        affected_xep = match.group(1)

    # makes it too long for fuzzy match :(
    # if affected_xep:
    #     tag = "{} {}".format(tag, affected_xep.upper())

    return URLMetadata(
        matched_url=matched_url,
        title="[PR#{}] {}".format(num, title),
        description=description,
        tag=tag,
        urls=[
            XEPS_PR_HTML_URL_TEMPLATE.format(num=num)
        ]
    )


async def _extract_standards_metadata(match):
    STANDARDS_PREFIX = "[Standards] "

    matched_url = match.group(0)

    async with aiohttp.ClientSession() as session:
        async with session.get(matched_url) as response:
            data = await response.content.read(MAX_READ_SIZE)

    soup = bs4.BeautifulSoup(data, "lxml")
    del data

    title = soup.find("h1").text
    if title.startswith(STANDARDS_PREFIX):
        title = title[len(STANDARDS_PREFIX):]

    return URLMetadata(
        matched_url=matched_url,
        title=title.strip() or None,
        description=None,
        tag=None,
        urls=[matched_url]
    )


_IMPLEMENTATIONS = [
    (PROTOXEP_URL_RE, _extract_protoxep_metadata),
    (XEPS_PR_URL_RE, _extract_xeps_pr_metadata),
    (STANDARDS_URL_RE, _extract_standards_metadata),
]


async def extract_url_metadata(url):
    for match_re, extractor in _IMPLEMENTATIONS:
        match = match_re.search(url)
        if match is None:
            continue

        return (await extractor(match))

    return None
