import json

import pytest
import requests

import db
import providers as P


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data if data is not None else {}, headers or {}

    def json(self):
        if self._data == "BAD":
            raise ValueError("bad json")
        return self._data


class FakeHTTP:
    """Queue of canned responses (or exceptions). Counts calls."""

    def __init__(self, *queue):
        self.queue, self.calls = list(queue), 0

    def _next(self, *a, **k):
        self.calls += 1
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    get = post = _next


def tavily_ok():
    return Resp(200, {"results": [{"title": "Pune Hackathon", "url": "https://a.com/h", "content": "48h"}]})


@pytest.fixture
def keys(monkeypatch):
    for k in ("GOOGLE_CSE_KEY", "GOOGLE_CSE_CX", "TAVILY_API_KEY", "BRAVE_API_KEY", "SERPER_API_KEY"):
        monkeypatch.setenv(k, "x")


def status_of(name):
    return P._usage_row(name)["status"]


def test_429_is_limit_but_500_and_timeout_are_error(keys):
    t = P.Tavily(FakeHTTP(Resp(429)))
    with pytest.raises(P.ProviderError) as e:
        t.search("q")
    assert e.value.kind == "limit"
    for bad in (Resp(500), Resp(503), requests.Timeout(), requests.ConnectionError()):
        with pytest.raises(P.ProviderError) as e:
            P.Tavily(FakeHTTP(bad)).search("q")
        assert e.value.kind == "transient"


def test_auth_errors_and_google_quota_body(keys):
    with pytest.raises(P.ProviderError) as e:
        P.Tavily(FakeHTTP(Resp(401))).search("q")
    assert e.value.kind == "auth"
    quota = Resp(403, {"error": {"errors": [{"reason": "dailyLimitExceeded"}]}})
    with pytest.raises(P.ProviderError) as e:
        P.GoogleCSE(FakeHTTP(quota)).search("q")
    assert e.value.kind == "limit"
    denied = Resp(403, {"error": {"errors": [{"reason": "forbidden"}]}})
    with pytest.raises(P.ProviderError) as e:
        P.GoogleCSE(FakeHTTP(denied)).search("q")
    assert e.value.kind == "auth"


def test_serper_credit_exhaustion_and_tavily_plan_limit(keys):
    with pytest.raises(P.ProviderError) as e:
        P.Serper(FakeHTTP(Resp(400, {"message": "Not enough credits"}))).search("q")
    assert e.value.kind == "limit"
    with pytest.raises(P.ProviderError) as e:
        P.Tavily(FakeHTTP(Resp(432))).search("q")
    assert e.value.kind == "limit"


def test_brave_reports_exact_remaining_and_reset(keys):
    h = {"X-RateLimit-Remaining": "1, 1999", "X-RateLimit-Reset": "1, 86400"}
    b = P.Brave(FakeHTTP(Resp(200, {"web": {"results": [{"title": "t", "url": "https://a.com", "description": "d"}]}}, h)))
    mgr = P.SearchManager([b])
    results, name = mgr.search("q")
    assert name == "brave" and results[0]["title"] == "t"
    snap = {r["provider"]: r for r in P.snapshot([b])["providers"]}["brave"]
    assert snap["remaining"] == 1999 and snap["remaining_exact"] is True and snap["reset_at"]


def test_estimate_is_labelled_estimate_and_unknown_by_default(keys, monkeypatch):
    t = P.Tavily(FakeHTTP(tavily_ok()))
    P.SearchManager([t]).search("q")
    row = {r["provider"]: r for r in P.snapshot([t])["providers"]}["tavily"]
    assert row["remaining"] is None and row["estimate"] is None  # nothing reported, nothing configured
    monkeypatch.setenv("TAVILY_EST_LIMIT", "10")
    row = {r["provider"]: r for r in P.snapshot([t])["providers"]}["tavily"]
    assert row["remaining"] is None and row["estimate"]["remaining"] == 9


def test_fallback_when_first_provider_hits_limit(keys):
    a, b = P.GoogleCSE(FakeHTTP(Resp(429))), P.Tavily(FakeHTTP(tavily_ok()))
    mgr = P.SearchManager([a, b])
    _, name = mgr.search("q")
    assert name == "tavily" and status_of("google_cse") == P.LIMIT_REACHED and status_of("tavily") == P.AVAILABLE
    # the limited provider is not called again
    mgr.search("q2")
    assert a.http.calls == 1


def test_fallback_on_transient_failure_but_status_is_error_not_limit(keys):
    a, b = P.GoogleCSE(FakeHTTP(Resp(503))), P.Tavily(FakeHTTP(tavily_ok()))
    _, name = P.SearchManager([a, b]).search("q")
    assert name == "tavily" and status_of("google_cse") == P.ERROR
    assert P.is_eligible(a) is True  # transient problems are retried on a later run


def test_auth_error_stops_provider_until_recheck(keys):
    a = P.Tavily(FakeHTTP(Resp(401)))
    b = P.Brave(FakeHTTP(Resp(200, {"web": {"results": []}})))
    mgr = P.SearchManager([a, b])
    mgr.search("q")
    assert P.is_eligible(a) is False and a.http.calls == 1
    mgr.search("q2")
    assert a.http.calls == 1


def test_all_limited_stops_all_requests_and_reports_state(keys):
    a, b = P.GoogleCSE(FakeHTTP(Resp(429))), P.Tavily(FakeHTTP(Resp(429)))
    mgr = P.SearchManager([a, b])
    with pytest.raises(P.AllProvidersExhausted):
        mgr.search("q")
    assert P.snapshot([a, b])["overall"] == "ALL_PROVIDERS_LIMIT_REACHED"
    calls = (a.http.calls, b.http.calls)
    with pytest.raises(P.AllProvidersExhausted):
        mgr.search("q again")
    assert (a.http.calls, b.http.calls) == calls  # zero new requests once everyone is limited
    assert mgr.has_budget() is False


def test_recheck_clears_limit_when_provider_recovers(keys):
    t = P.Tavily(FakeHTTP(Resp(429), tavily_ok()))
    mgr = P.SearchManager([t])
    with pytest.raises(P.AllProvidersExhausted):
        mgr.search("q")
    assert status_of("tavily") == P.LIMIT_REACHED
    mgr.recheck("tavily")
    assert status_of("tavily") == P.AVAILABLE and P.is_eligible(t)


def test_provider_reset_time_in_the_past_makes_it_eligible_again(keys):
    t = P.Tavily(FakeHTTP(Resp(429, headers={"Retry-After": "60"})))
    mgr = P.SearchManager([t])
    with pytest.raises(P.AllProvidersExhausted):
        mgr.search("q")
    assert P._usage_row("tavily")["reset_at"]
    with db.connect() as c:
        c.execute("UPDATE provider_usage SET reset_at='2000-01-01T00:00:00+00:00' WHERE provider='tavily'")
    assert P.is_eligible(t)


def test_no_keys_means_no_providers_and_unconfigured_rows_are_labelled():
    mgr = P.SearchManager()
    with pytest.raises(P.NoProvidersConfigured):
        mgr.search("q")
    snap = P.snapshot(mgr.providers)
    assert snap["overall"] == "NO_PROVIDERS_CONFIGURED"
    assert {r["status"] for r in snap["providers"]} == {"NOT_CONFIGURED"}


def test_usage_counters(keys):
    a, b = P.GoogleCSE(FakeHTTP(Resp(429))), P.Tavily(FakeHTTP(tavily_ok()))
    P.SearchManager([a, b]).search("q")
    rows = {r["provider"]: r for r in P.snapshot([a, b])["providers"]}
    assert (rows["google_cse"]["requests"], rows["google_cse"]["failures"], rows["google_cse"]["last_http_status"]) == (1, 1, 429)
    assert (rows["tavily"]["requests"], rows["tavily"]["successes"]) == (1, 1)


def test_all_failing_for_non_quota_reasons_is_not_reported_as_limit(keys):
    a, b = P.GoogleCSE(FakeHTTP(Resp(503))), P.Tavily(FakeHTTP(requests.Timeout()))
    mgr = P.SearchManager([a, b])
    with pytest.raises(P.ProvidersFailed):
        mgr.search("q")
    assert P.snapshot([a, b])["overall"] == "OK"      # not ALL_PROVIDERS_LIMIT_REACHED
    assert {status_of("google_cse"), status_of("tavily")} == {P.ERROR}
