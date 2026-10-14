"""Polite page fetcher: robots.txt aware, rate-limited per domain, size-capped."""
import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlsplit

import requests

from urlnorm import domain_of

USER_AGENT = "OpportunityRadar/1.0 (personal dashboard; manual runs; +local)"
MAX_BYTES = 1_500_000

# Not a source list. Platforms whose terms forbid automated access, so pages are never FETCHED.
# A search API may still return their links and those are shown as plain links.
NO_FETCH_DOMAINS = {
    "linkedin.com", "naukri.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "glassdoor.com", "glassdoor.co.in", "indeed.com", "quora.com", "reddit.com", "whatsapp.com", "t.me",
}


@dataclass
class FetchResult:
    url: str
    status: int = 0
    content_type: str = ""
    text: str = ""
    error: str = ""
    robots_blocked: bool = False
    skipped: bool = False

    @property
    def ok(self):
        return 200 <= self.status < 300 and bool(self.text)


def _suffix_match(domain, banned):
    return any(domain == b or domain.endswith("." + b) for b in banned)


class Fetcher:
    def __init__(self, http=None, delay=1.0):
        self.http = http or requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9"})
        self.delay = delay
        self._robots = {}
        self._last = {}

    def _wait(self, domain):
        gap = self.delay - (time.monotonic() - self._last.get(domain, 0))
        if gap > 0:
            time.sleep(gap)
        self._last[domain] = time.monotonic()

    def _robots_for(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        rp = robotparser.RobotFileParser()
        try:
            self._wait(parts.netloc)
            r = self.http.get(origin + "/robots.txt", timeout=8)
            if r.status_code >= 500:
                rp.disallow_all = True        # RFC 9309: server errors mean "do not crawl"
            elif r.status_code >= 400:
                rp.allow_all = True           # no robots.txt means no restrictions
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            rp.disallow_all = True
        self._robots[origin] = rp
        return rp

    def get(self, url) -> FetchResult:
        domain = domain_of(url)
        if not url.startswith(("http://", "https://")):
            return FetchResult(url, error="unsupported scheme", skipped=True)
        if _suffix_match(domain, NO_FETCH_DOMAINS):
            return FetchResult(url, error="domain does not allow automated access", skipped=True)
        if not self._robots_for(url).can_fetch(USER_AGENT, url):
            return FetchResult(url, error="blocked by robots.txt", robots_blocked=True, skipped=True)
        try:
            self._wait(domain)
            r = self.http.get(url, timeout=12, stream=True)
            ctype = r.headers.get("Content-Type", "").lower()
            if not any(t in ctype for t in ("html", "xml", "text/plain")):
                return FetchResult(url, r.status_code, ctype, error="not a text page", skipped=True)
            body = r.raw.read(MAX_BYTES, decode_content=True) if hasattr(r, "raw") and r.raw else r.content[:MAX_BYTES]
            enc = r.encoding or "utf-8"
            return FetchResult(url, r.status_code, ctype, body.decode(enc, errors="replace"))
        except requests.RequestException as exc:
            return FetchResult(url, error=f"{type(exc).__name__}")
