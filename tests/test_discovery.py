import json

import pytest

import db
import discover
import providers as P
from fetch import FetchResult, Fetcher


# ---------------------------------------------------------------- fakes
class FakeManager:
    def __init__(self, per_query=None, raise_exc=None):
        self.per_query, self.raise_exc, self.queries = per_query or (lambda q: []), raise_exc, []

    def search(self, query, count=10):
        self.queries.append(query)
        if self.raise_exc:
            raise self.raise_exc
        return self.per_query(query), "fake"


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.fetched = pages, []

    def get(self, url):
        self.fetched.append(url)
        if url in self.pages:
            return FetchResult(url, 200, "text/html", self.pages[url])
        return FetchResult(url, error="not found")


def r(title, url, snippet=""):
    return {"title": title, "url": url, "snippet": snippet, "published": None}


HACK_PAGE = """<html><head><title>Pune AI Hackathon</title>
<link rel="alternate" type="application/rss+xml" href="https://punehack.example.org/feed.xml"></head>
<body><p>Registration deadline: 20 November 2026</p>""" + "".join(
    f'<a href="/h/{i}">Mumbai Data Science Hackathon edition {i} registration open</a>' for i in range(14)) + "</body></html>"

EVENT_PAGE = """<html><head><script type="application/ld+json">{"@type":"Event","name":"LLM Night",
"startDate":"2026-11-14","location":{"address":{"addressLocality":"Mumbai"}}}</script></head></html>"""

FEED = """<rss><channel>
<item><title>Pune Hackathon for AI students - register</title><link>https://punehack.example.org/h/new</link>
<pubDate>Tue, 13 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Our company picnic</title><link>https://punehack.example.org/picnic</link></item>
</channel></rss>"""


def canned(q):
    ql = q.lower()
    if "hackathon" in ql:
        return [r("Pune AI Hackathon 2026 - register now", "https://punehack.example.org/h1?utm_source=x", "48 hour hackathon for students in Pune"),
                r("Best pizza recipes", "https://recipes.example.com/pizza", "easy dinner ideas")]
    if "intern" in ql or "fresher" in ql:
        return [r("Machine Learning Intern (Freshers)", "https://jobs.example.net/ml-intern", "AI internship for freshers")]
    if "meetup" in ql or "events" in ql:
        return [r("Mumbai Developer Meetup: LLM Night", "https://mumbaitech.example.org/e/1", "tech meetup in Mumbai")]
    if "free" in ql:
        return [r("Free GPU credits for students", "https://credits.example.dev/gpu", "free cloud credits for machine learning")]
    return []


PAGES = {"https://punehack.example.org/h1?utm_source=x": HACK_PAGE,
         "https://mumbaitech.example.org/e/1": EVENT_PAGE,
         "https://punehack.example.org/feed.xml": FEED}


def run(manager, fetcher):
    return discover.Discovery(manager, fetcher).run()


def test_full_run_discovers_classifies_learns_sources_and_never_invents_dates():
    fetcher = FakeFetcher(PAGES)
    out = run(FakeManager(canned), fetcher)
    assert out["outcome"] == "ok" and out["items_new"] > 0
    items = {i["title"]: i for i in db.list_items()}
    assert set(i["category"] for i in items.values()) == {"hackathon", "job", "event", "resource"}
    assert "Best pizza recipes" not in items                       # irrelevant result rejected
    hack = items["Pune AI Hackathon 2026 - register now"]
    assert hack["deadline"] == "2026-11-20" and hack["metadata"]["deadline_source"] == "found in page text"
    assert hack["city"] == "Pune"
    ev = items["Mumbai Developer Meetup: LLM Night"]
    assert ev["event_date"] == "2026-11-14" and ev["deadline"] is None   # event date is not a "deadline"
    job = items["Machine Learning Intern (Freshers)"]
    assert job["deadline"] is None and job["published_at"] is None and job["event_date"] is None
    assert not any("utm_source" in i["norm_url"] for i in items.values())
    # listing page harvest
    assert any(i["found_via"].startswith("listing:") for i in items.values())
    # sources were learned from results, with no seed list
    src = {s["domain"]: s for s in db.list_sources()}
    assert "punehack.example.org" in src and src["punehack.example.org"]["feed_urls"] == ["https://punehack.example.org/feed.xml"]
    assert src["recipes.example.com"]["misses"] >= 1 and src["recipes.example.com"]["hits"] == 0


def test_rediscovery_dedupes_and_keeps_pins():
    run(FakeManager(canned), FakeFetcher(PAGES))
    first = db.list_items()
    pin_id = first[0]["id"]
    db.set_pinned(pin_id, True)
    run(FakeManager(canned), FakeFetcher(PAGES))
    again = db.list_items()
    urls = [i["norm_url"] for i in again]
    assert len(urls) == len(set(urls))                         # no duplicates created
    assert {i["norm_url"] for i in first} <= set(urls)         # nothing lost (the learned feed may add new items)
    assert [i for i in again if i["id"] == pin_id][0]["pinned"] is True


def test_learned_feed_is_polled_next_run_even_when_all_providers_are_limited():
    run(FakeManager(canned), FakeFetcher(PAGES))
    before = len(db.list_items())
    fetcher = FakeFetcher(PAGES)
    out = run(FakeManager(raise_exc=P.AllProvidersExhausted("x")), fetcher)
    assert out["outcome"] == "limit" and "ALL PROVIDERS LIMIT REACHED" in out["message"]
    assert "https://punehack.example.org/feed.xml" in fetcher.fetched
    titles = [i["title"] for i in db.list_items()]
    assert "Pune Hackathon for AI students - register" in titles and "Our company picnic" not in titles
    assert len(db.list_items()) == before + 1


def test_no_providers_is_reported_clearly():
    out = run(FakeManager(raise_exc=P.NoProvidersConfigured("none")), FakeFetcher({}))
    assert out["outcome"] == "no_providers" and ".env" in out["message"]


def test_empty_results_are_handled_gracefully():
    out = run(FakeManager(lambda q: []), FakeFetcher({}))
    assert out["outcome"] == "ok" and out["items_seen"] == 0 and "nothing relevant" in out["message"]


def test_noisy_sources_get_muted_and_ignored():
    junk = lambda q: [r(f"Pizza number {i}", f"https://spam.example.com/p{i}", "recipes") for i in range(10)]
    run(FakeManager(junk), FakeFetcher({}))
    assert {s["domain"]: s for s in db.list_sources()}["spam.example.com"]["muted"] == 1


def test_queries_are_goal_templates_and_rotate_between_runs():
    m1, m2 = FakeManager(), FakeManager()
    run(m1, FakeFetcher({}))
    run(m2, FakeManager and FakeFetcher({}))
    assert m1.queries and set(m1.queries) != set(m2.queries)
    joined = " ".join(m1.queries).lower()
    assert "http" not in joined and ".com" not in joined       # goals only, never sites


def test_exceptions_surface_as_error_state():
    class Boom(FakeManager):
        def search(self, q, count=10):
            raise RuntimeError("kaboom")
    out = run(Boom(), FakeFetcher({}))
    assert out["outcome"] == "error" and "kaboom" in out["message"]


# ---------------------------------------------------------------- polite fetcher
class RawBody:
    def __init__(self, b): self.b = b
    def read(self, n, decode_content=True): return self.b[:n]


class WebResp:
    def __init__(self, status=200, body="", ctype="text/html"):
        self.status_code, self.headers, self.encoding = status, {"Content-Type": ctype}, "utf-8"
        self.text, self.raw = body, RawBody(body.encode())


class WebHTTP:
    def __init__(self, routes): self.routes, self.headers, self.hits = routes, {}, []
    def get(self, url, **kw):
        self.hits.append(url)
        return self.routes.get(url, WebResp(404))


def test_fetcher_respects_robots_and_never_fetches_login_walled_platforms():
    http = WebHTTP({"https://ok.example.com/robots.txt": WebResp(200, "User-agent: *\nDisallow: /private"),
                    "https://ok.example.com/public": WebResp(200, "<html>hi</html>"),
                    "https://ok.example.com/private/x": WebResp(200, "secret")})
    f = Fetcher(http, delay=0)
    assert f.get("https://ok.example.com/public").ok
    blocked = f.get("https://ok.example.com/private/x")
    assert blocked.robots_blocked and "https://ok.example.com/private/x" not in http.hits
    li = f.get("https://www.linkedin.com/jobs/view/1")
    assert li.skipped and not any("linkedin" in h for h in http.hits)


def test_robots_server_error_means_do_not_crawl():
    http = WebHTTP({"https://down.example.com/robots.txt": WebResp(503)})
    res = Fetcher(http, delay=0).get("https://down.example.com/page")
    assert res.robots_blocked and "https://down.example.com/page" not in http.hits


def test_provider_errors_are_not_reported_as_quota_limit():
    out = run(FakeManager(raise_exc=P.ProvidersFailed("x")), FakeFetcher({}))
    assert out["outcome"] == "provider_error"
    assert "LIMIT" not in out["message"] and "not a quota limit" in out["message"]
