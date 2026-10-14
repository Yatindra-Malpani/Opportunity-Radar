"""Pull structured facts out of fetched pages and feeds. Standard library only."""
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin

from dates import parse_date, find_labeled_date

EVENT_TYPES = {"event", "educationevent", "businessevent", "hackathon", "socialevent", "courseinstance"}
JOB_TYPES = {"jobposting"}


class PageParser(HTMLParser):
    """Collects title, meta tags, JSON-LD blocks, feed links, anchors and visible text."""

    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.title = ""
        self.meta = {}
        self.jsonld = []
        self.feeds = []
        self.anchors = []
        self.text = []
        self._in_title = False
        self._in_ld = False
        self._ld_buf = []
        self._skip = 0
        self._a_href = None
        self._a_buf = []
        self._text_len = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            typ = (a.get("type") or "").lower()
            if "alternate" in rel and ("rss" in typ or "atom" in typ) and a.get("href"):
                self.feeds.append(urljoin(self.base, a["href"]))
        elif tag == "script":
            if (a.get("type") or "").lower() == "application/ld+json":
                self._in_ld, self._ld_buf = True, []
            else:
                self._skip += 1
        elif tag in ("style", "noscript", "svg"):
            self._skip += 1
        elif tag == "a" and a.get("href"):
            self._a_href, self._a_buf = a["href"], []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            if self._in_ld:
                self._in_ld = False
                self.jsonld.append("".join(self._ld_buf))
            elif self._skip:
                self._skip -= 1
        elif tag in ("style", "noscript", "svg") and self._skip:
            self._skip -= 1
        elif tag == "a" and self._a_href is not None:
            label = " ".join("".join(self._a_buf).split())
            if label:
                self.anchors.append((urljoin(self.base, self._a_href), label))
            self._a_href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_ld:
            self._ld_buf.append(data)
            return
        if self._skip:
            return
        if self._a_href is not None:
            self._a_buf.append(data)
        if self._text_len < 60000:
            s = data.strip()
            if s:
                self.text.append(s)
                self._text_len += len(s)


def _walk_jsonld(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from _walk_jsonld(node[key])


def _types(node):
    t = node.get("@type")
    if isinstance(t, str):
        t = [t]
    return {str(x).split("/")[-1].lower() for x in (t or [])}


def _name(v):
    if isinstance(v, dict):
        return v.get("name")
    if isinstance(v, list) and v:
        return _name(v[0])
    return v if isinstance(v, str) else None


def _locality(loc):
    """Return (city_text, is_online) from a schema.org location value."""
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return (loc if isinstance(loc, str) else None), False
    if str(loc.get("@type", "")).lower() == "virtuallocation":
        return None, True
    addr = loc.get("address")
    if isinstance(addr, dict):
        return addr.get("addressLocality") or addr.get("addressRegion") or loc.get("name"), False
    return (addr if isinstance(addr, str) else loc.get("name")), False


def structured_facts(parser: PageParser):
    """Return a list of dicts for Event / JobPosting blocks found on the page."""
    facts = []
    for raw in parser.jsonld:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for node in _walk_jsonld(data):
            types = _types(node)
            if types & EVENT_TYPES:
                city, online = _locality(node.get("location"))
                mode = str(node.get("eventAttendanceMode", "")).lower()
                facts.append({
                    "kind": "event", "name": node.get("name"),
                    "event_date": parse_date(str(node.get("startDate") or "")),
                    "end_date": parse_date(str(node.get("endDate") or "")),
                    "city_text": city, "online": online or "online" in mode and "mixed" not in mode,
                    "organizer": _name(node.get("organizer")), "url": node.get("url"),
                })
            elif types & JOB_TYPES:
                loc = node.get("jobLocation")
                city, _ = _locality(loc)
                facts.append({
                    "kind": "job", "name": node.get("title") or node.get("name"),
                    "published_at": parse_date(str(node.get("datePosted") or "")),
                    "deadline": parse_date(str(node.get("validThrough") or "")),
                    "city_text": city, "online": str(node.get("jobLocationType", "")).upper() == "TELECOMMUTE",
                    "organizer": _name(node.get("hiringOrganization")),
                    "employment": node.get("employmentType"), "url": node.get("url"),
                })
    return facts


def page_published(parser: PageParser):
    for key in ("article:published_time", "og:published_time", "datepublished", "date", "pubdate"):
        d = parse_date(parser.meta.get(key, ""))
        if d:
            return d
    return None


def analyse_page(html: str, url: str):
    """Parse a page once and return everything discovery needs from it."""
    p = PageParser(url)
    try:
        p.feed(html)
    except Exception:  # malformed HTML must never kill a run
        pass
    text = " ".join(p.text)
    return {
        "title": " ".join(p.title.split()) or p.meta.get("og:title"),
        "facts": structured_facts(p),
        "published_at": page_published(p),
        "text_deadline": find_labeled_date(text),
        "feeds": list(dict.fromkeys(p.feeds)),
        "anchors": p.anchors,
        "site_name": p.meta.get("og:site_name"),
    }


# ---------------------------------------------------------------- feeds

def _strip_ns(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text: str, limit=60):
    """Parse RSS 2.0 / Atom into [{title, url, published_at, summary}]."""
    if len(xml_text) > 2_000_000 or "<!ENTITY" in xml_text:  # refuse entity tricks and giant files
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if _strip_ns(el.tag) not in ("item", "entry"):
            continue
        title = link = date = summary = None
        for ch in el:
            t = _strip_ns(ch.tag)
            if t == "title":
                title = (ch.text or "").strip()
            elif t == "link":
                link = ch.attrib.get("href") or (ch.text or "").strip() or link
            elif t in ("pubDate", "published", "updated", "date") and not date:
                date = parse_date(ch.text or "")
            elif t in ("description", "summary") and not summary:
                summary = re.sub(r"<[^>]+>", " ", ch.text or "")
                summary = " ".join(summary.split())[:400]
        if title and link:
            out.append({"title": title, "url": link, "published_at": date, "summary": summary})
        if len(out) >= limit:
            break
    return out
