import db
from classify import classify
from dates import parse_date, find_labeled_date
from extract import analyse_page, parse_feed
from urlnorm import normalize_url

CITIES = ["Mumbai", "Pune"]


def test_url_normalization_collapses_variants():
    a = normalize_url("http://www.Example.com/events/ai-meetup/?utm_source=x&b=2&a=1#top")
    b = normalize_url("https://example.com/events/ai-meetup?a=1&b=2")
    assert a == b == "https://example.com/events/ai-meetup?a=1&b=2"


def test_dates_only_explicit():
    assert parse_date("2026-11-05T10:00:00+05:30") == "2026-11-05"
    assert parse_date("25th October 2026") == "2026-10-25"
    assert parse_date("Oct 25, 2026") == "2026-10-25"
    assert parse_date("Tue, 14 Oct 2026 08:00:00 GMT") == "2026-10-14"
    assert parse_date("2 days ago") is None
    assert parse_date("next Saturday") is None
    assert parse_date("") is None


def test_labeled_deadline_needs_label_and_year():
    assert find_labeled_date("Registration deadline: 30 November 2026. Prizes...") == "2026-11-30"
    assert find_labeled_date("Apply by Dec 5, 2026") == "2026-12-05"
    assert find_labeled_date("Event on 30 November 2026") is None
    assert find_labeled_date("Deadline: soon") is None


def test_classifier_accepts_and_rejects():
    ok = classify("Pune AI Hackathon 2026 - Register now", "48 hour hackathon for students", "https://x.org/h", "hackathon", CITIES)
    assert ok and ok["category"] == "hackathon" and ok["city"] == "Pune"
    online = classify("Global online hackathon", "Open to everyone, virtual", "https://x.org/h", "hackathon", CITIES)
    assert online and online["city"] == "Online"
    elsewhere = classify("Bangalore hackathon for developers", "in person at Koramangala", "https://x.org/h", "hackathon", CITIES)
    assert elsewhere is None
    gov = classify("Smart India Hackathon 2026", "Ministry of Education initiative", "https://sih.gov.in", "hackathon", CITIES)
    assert gov and "government" in gov["tags"]

    job = classify("Machine Learning Intern - Freshers welcome", "Apply for AI internship", "https://c.com/j", "job", CITIES)
    assert job and job["category"] == "job"
    assert classify("Senior Machine Learning Engineer, 7+ years", "Lead our AI team", "https://c.com/j", "job", CITIES) is None
    assert classify("Sales intern wanted", "Fresher roles in marketing", "https://c.com/j", "job", CITIES) is None

    ev = classify("Mumbai Developer Meetup: Intro to LLMs", "Join the community on 12 Nov", "https://meetup.com/m", "event", CITIES)
    assert ev and ev["category"] == "event" and ev["city"] == "Mumbai"
    assert classify("Online AI webinar", "Learn from anywhere", "https://x.com/w", "event", CITIES) is None  # events are city-bound
    assert classify("Pune food festival", "Biryani and chaat", "https://x.com/f", "event", CITIES) is None

    res = classify("Free GPU credits for students", "Get free cloud credits for machine learning", "https://x.com/c", "resource", CITIES)
    assert res and res["category"] == "resource"
    assert classify("Best pizza recipes", "easy dinner ideas", "https://x.com/p", "resource", CITIES) is None


EVENT_HTML = """<html><head><title>AI Meetup Pune</title>
<meta property="article:published_time" content="2026-10-01T09:00:00Z">
<meta property="og:site_name" content="Pune Tech Community">
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Event","name":"AI Meetup Pune",
"startDate":"2026-11-08T16:00:00+05:30","location":{"@type":"Place","name":"Hub","address":{"addressLocality":"Pune"}},
"organizer":{"@type":"Organization","name":"Pune AI Group"}}</script></head>
<body><p>Registration deadline: 5 November 2026</p></body></html>"""

JOB_HTML = """<html><head><title>ML Intern</title><script type="application/ld+json">
{"@graph":[{"@type":"JobPosting","title":"ML Intern","datePosted":"2026-10-10","validThrough":"2026-11-30T00:00",
"hiringOrganization":{"name":"Acme AI"},"jobLocation":{"address":{"addressLocality":"Mumbai"}},"employmentType":"INTERN"}]}
</script></head></html>"""


def test_extract_event_and_job_and_feed_link():
    p = analyse_page(EVENT_HTML, "https://pune.example.org/m/1")
    f = p["facts"][0]
    assert f["event_date"] == "2026-11-08" and f["city_text"] == "Pune" and f["organizer"] == "Pune AI Group"
    assert p["published_at"] == "2026-10-01" and p["text_deadline"] == "2026-11-05"
    assert p["feeds"] == ["https://pune.example.org/feed.xml"] and p["site_name"] == "Pune Tech Community"
    j = analyse_page(JOB_HTML, "https://acme.ai/j")["facts"][0]
    assert j["kind"] == "job" and j["deadline"] == "2026-11-30" and j["published_at"] == "2026-10-10"


def test_malformed_html_does_not_crash():
    analyse_page("<html><head><script type='application/ld+json'>{not json</script><title>x", "https://a.com")


def test_parse_feed_rss_and_atom_and_entity_guard():
    rss = """<rss><channel><item><title>Hackathon X</title><link>https://a.com/x</link>
    <pubDate>Tue, 14 Oct 2026 08:00:00 GMT</pubDate><description>&lt;b&gt;48h&lt;/b&gt; build</description></item></channel></rss>"""
    e = parse_feed(rss)[0]
    assert e["title"] == "Hackathon X" and e["published_at"] == "2026-10-14" and "<" not in e["summary"]
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Meetup</title>
    <link href="https://a.com/m"/><updated>2026-10-12T10:00:00Z</updated></entry></feed>"""
    assert parse_feed(atom)[0]["url"] == "https://a.com/m"
    assert parse_feed('<!DOCTYPE x [<!ENTITY a "b">]><rss/>') == []


def test_dedupe_pin_persistence_and_unpin_keeps_record():
    item = {"norm_url": "https://a.com/x", "url": "https://a.com/x", "title": "T", "category": "hackathon", "domain": "a.com"}
    assert db.upsert_item(item) is True
    assert db.upsert_item({**item, "city": "Pune"}) is False
    rows = db.list_items()
    assert len(rows) == 1 and rows[0]["city"] == "Pune" and rows[0]["seen_count"] == 2
    db.set_pinned(rows[0]["id"], True)
    db.upsert_item(item)  # rediscovery must not unpin
    assert db.list_items(pinned_only=True)[0]["pinned"] is True
    db.set_pinned(rows[0]["id"], False)
    assert db.list_items(pinned_only=True) == [] and len(db.list_items()) == 1


def test_missing_dates_are_not_invented_and_sort_last():
    base = {"url": "https://a.com/1", "title": "t", "category": "event", "domain": "a.com"}
    db.upsert_item({**base, "norm_url": "https://a.com/1", "event_date": "2099-01-02"})
    db.upsert_item({**base, "norm_url": "https://a.com/2", "url": "https://a.com/2"})
    db.upsert_item({**base, "norm_url": "https://a.com/3", "url": "https://a.com/3", "deadline": "2099-01-01"})
    order = [r["norm_url"][-1] for r in db.list_items(sort="deadline")]
    assert order == ["3", "1", "2"]
    assert all(r["deadline"] is None for r in db.list_items() if r["norm_url"].endswith("2"))
    assert len(db.list_items(deadline="none")) == 1
