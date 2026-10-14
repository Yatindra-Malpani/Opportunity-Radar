"""Free-tier search providers, usage tracking and safe fallback.

Rules (from the spec):
  * Only providers with an API key in .env are used. Nothing paid is ever enabled here.
  * LIMIT_REACHED is set only when the provider itself confirms a quota/rate limit
    (HTTP 429, or a documented quota error body). Timeouts and 5xx are ERROR.
  * Remaining quota is shown as exact only when the provider reports it; otherwise
    it is an estimate from the local request log, or unknown.
  * If every configured provider is LIMIT_REACHED, no further search requests are made.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

import db

AVAILABLE, LIMIT_REACHED, ERROR, UNKNOWN = "AVAILABLE", "LIMIT_REACHED", "ERROR", "UNKNOWN"
TIMEOUT = 15


class ProviderError(Exception):
    """kind: 'limit' (confirmed quota), 'auth' (needs the user), 'transient' (retry later)."""

    def __init__(self, kind, message, http_status=None, reset_at=None):
        super().__init__(message)
        self.kind, self.http_status, self.reset_at = kind, http_status, reset_at


class AllProvidersExhausted(Exception):
    pass


class NoProvidersConfigured(Exception):
    pass


class ProvidersFailed(Exception):
    """Eligible providers were tried and all failed for reasons other than quota."""


def _iso_in(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def _retry_after(resp):
    try:
        return _iso_in(int(resp.headers.get("Retry-After", "")))
    except ValueError:
        return None


class Provider:
    name = "base"
    key_env = ""
    extra_env = ()

    def __init__(self, http=None):
        self.http = http or requests.Session()
        self.last_quota = {}

    @property
    def configured(self):
        return all(os.environ.get(k) for k in (self.key_env, *self.extra_env))

    @property
    def key(self):
        return os.environ.get(self.key_env, "")

    # subclasses implement _request and _parse
    def search(self, query, count=10):
        self.last_quota = {}
        try:
            resp = self._request(query, count)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise ProviderError("transient", f"Network error: {type(exc).__name__}") from exc
        self._read_quota(resp)
        self._raise_for_status(resp)
        try:
            return self._parse(resp.json())
        except ValueError as exc:
            raise ProviderError("transient", "Provider returned invalid JSON", resp.status_code) from exc

    def _read_quota(self, resp):
        pass

    def _is_quota_body(self, resp):
        return False

    def _raise_for_status(self, resp):
        s = resp.status_code
        if 200 <= s < 300:
            return
        if s == 429 or self._is_quota_body(resp):
            raise ProviderError("limit", f"HTTP {s}: quota / rate limit reached", s,
                                reset_at=self.last_quota.get("reset_at") or _retry_after(resp))
        if s in (401, 403):
            raise ProviderError("auth", f"HTTP {s}: key rejected or not permitted", s)
        if s >= 500 or s in (408, 425):
            raise ProviderError("transient", f"HTTP {s}: provider server problem", s)
        raise ProviderError("auth", f"HTTP {s}: request not accepted (check configuration)", s)


def _result(title, url, snippet=None, published=None):
    return {"title": (title or "").strip(), "url": (url or "").strip(),
            "snippet": (snippet or "").strip() or None, "published": published}


class GoogleCSE(Provider):
    name = "google_cse"
    key_env = "GOOGLE_CSE_KEY"
    extra_env = ("GOOGLE_CSE_CX",)

    def _request(self, q, n):
        return self.http.get("https://www.googleapis.com/customsearch/v1", timeout=TIMEOUT, params={
            "key": self.key, "cx": os.environ["GOOGLE_CSE_CX"], "q": q, "num": min(n, 10), "gl": "in"})

    def _is_quota_body(self, resp):
        try:
            err = resp.json().get("error", {})
        except ValueError:
            return False
        reasons = {e.get("reason") for e in err.get("errors", [])}
        return bool(reasons & {"dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded",
                               "quotaExceeded"}) or err.get("status") == "RESOURCE_EXHAUSTED"

    def _parse(self, data):
        out = []
        for it in data.get("items", []):
            meta = ((it.get("pagemap") or {}).get("metatags") or [{}])[0]
            out.append(_result(it.get("title"), it.get("link"), it.get("snippet"),
                               meta.get("article:published_time")))
        return out


class Tavily(Provider):
    name = "tavily"
    key_env = "TAVILY_API_KEY"

    def _request(self, q, n):
        # include_answer stays off: the spec forbids AI-generated summaries.
        return self.http.post("https://api.tavily.com/search", timeout=TIMEOUT,
                              headers={"Authorization": f"Bearer {self.key}"},
                              json={"query": q, "max_results": min(n, 20), "search_depth": "basic",
                                    "include_answer": False})

    def _is_quota_body(self, resp):
        return resp.status_code in (432, 433)  # documented plan / pay-go limit codes

    def _parse(self, data):
        return [_result(r.get("title"), r.get("url"), r.get("content"), r.get("published_date"))
                for r in data.get("results", [])]


class Brave(Provider):
    name = "brave"
    key_env = "BRAVE_API_KEY"

    def _request(self, q, n):
        return self.http.get("https://api.search.brave.com/res/v1/web/search", timeout=TIMEOUT,
                             headers={"X-Subscription-Token": self.key, "Accept": "application/json"},
                             params={"q": q, "count": min(n, 20), "country": "IN"})

    def _read_quota(self, resp):
        # Brave reports "<short window>, <long window>" values; the last is the longest window.
        rem, reset = resp.headers.get("X-RateLimit-Remaining"), resp.headers.get("X-RateLimit-Reset")
        try:
            if rem:
                self.last_quota["remaining"] = int(rem.split(",")[-1].strip())
            if reset:
                self.last_quota["reset_at"] = _iso_in(int(reset.split(",")[-1].strip()))
        except ValueError:
            self.last_quota = {}

    def _parse(self, data):
        return [_result(r.get("title"), r.get("url"), r.get("description"), r.get("page_age"))
                for r in (data.get("web") or {}).get("results", [])]


class Serper(Provider):
    name = "serper"
    key_env = "SERPER_API_KEY"

    def _request(self, q, n):
        return self.http.post("https://google.serper.dev/search", timeout=TIMEOUT,
                              headers={"X-API-KEY": self.key},
                              json={"q": q, "num": min(n, 10), "gl": "in"})

    def _is_quota_body(self, resp):
        try:
            return "credit" in str(resp.json().get("message", "")).lower()
        except ValueError:
            return False

    def _parse(self, data):
        return [_result(r.get("title"), r.get("link"), r.get("snippet"), r.get("date"))
                for r in data.get("organic", [])]


# Google's Custom Search JSON API is closed to new customers and being retired (existing users: 1 Jan 2027),
# so it is kept only as an optional legacy provider and tried last.
ALL_PROVIDERS = [Tavily, Brave, Serper, GoogleCSE]


def build_providers():
    order = [x.strip() for x in os.environ.get("PROVIDER_ORDER", "").split(",") if x.strip()]
    provs = [cls() for cls in ALL_PROVIDERS]
    if order:
        provs.sort(key=lambda p: order.index(p.name) if p.name in order else 99)
    return provs


# ------------------------------------------------------------- usage tracking

def _usage_row(name):
    with db.connect() as c:
        c.execute("INSERT OR IGNORE INTO provider_usage(provider) VALUES (?)", (name,))
        return dict(c.execute("SELECT * FROM provider_usage WHERE provider=?", (name,)).fetchone())


def record(name, query, outcome, http_status=None, detail=None, quota=None, error_kind=None, reset_at=None):
    """outcome: ok | limit | auth | transient"""
    quota = quota or {}
    _usage_row(name)
    now = db.now_iso()
    with db.connect() as c:
        c.execute("INSERT INTO provider_log(provider, at, query, http_status, outcome, detail) VALUES (?,?,?,?,?,?)",
                  (name, now, query, http_status, outcome, detail))
        if outcome == "ok":
            status = AVAILABLE
        elif outcome == "limit":
            status = LIMIT_REACHED
        else:
            status = ERROR
        c.execute(
            """UPDATE provider_usage SET status=?, requests=requests+1,
                   successes=successes+?, failures=failures+?, last_http_status=?, last_error=?,
                   error_kind=?, last_used=? WHERE provider=?""",
            (status, 1 if outcome == "ok" else 0, 0 if outcome == "ok" else 1,
             http_status, None if outcome == "ok" else detail, error_kind, now, name))
        if "remaining" in quota:
            c.execute("UPDATE provider_usage SET remaining=?, remaining_exact=1 WHERE provider=?",
                      (quota["remaining"], name))
        if outcome == "limit":
            c.execute("UPDATE provider_usage SET reset_at=?, remaining=COALESCE(?, 0), remaining_exact=? WHERE provider=?",
                      (reset_at, quota.get("remaining"), 1 if "remaining" in quota else 0, name))
        elif outcome == "ok" and quota.get("reset_at"):
            c.execute("UPDATE provider_usage SET reset_at=? WHERE provider=?", (quota["reset_at"], name))


def _window_start(window):
    now = datetime.now(timezone.utc)
    if window == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def estimate(name):
    """Estimated remaining from the local log. Only if the user set <NAME>_EST_LIMIT in .env."""
    env = name.upper()
    try:
        limit = int(os.environ.get(f"{env}_EST_LIMIT", ""))
    except ValueError:
        return None
    window = os.environ.get(f"{env}_EST_WINDOW", "month").lower()
    with db.connect() as c:
        used = c.execute("SELECT COUNT(*) n FROM provider_log WHERE provider=? AND at>=?",
                         (name, _window_start(window))).fetchone()["n"]
    return {"limit": limit, "window": window, "used": used, "remaining": max(0, limit - used)}


def _reset_passed(row):
    return bool(row["reset_at"]) and row["reset_at"] <= db.now_iso()


def is_eligible(p):
    if not p.configured:
        return False
    row = _usage_row(p.name)
    if row["status"] == LIMIT_REACHED:
        return _reset_passed(row)  # provider-supplied reset time has passed; otherwise wait for manual recheck
    if row["status"] == ERROR and row["error_kind"] == "auth":
        return False  # needs the user to fix the key; do not hammer the API
    return True


def snapshot(providers):
    """What the API Usage panel shows."""
    rows, configured, limited = [], 0, 0
    for p in providers:
        row = _usage_row(p.name)
        if p.configured:
            configured += 1
            if row["status"] == LIMIT_REACHED and not _reset_passed(row):
                limited += 1
        status = row["status"] if p.configured else "NOT_CONFIGURED"
        est = estimate(p.name) if p.configured else None
        rows.append({
            "provider": p.name, "status": status, "configured": p.configured,
            "requests": row["requests"], "successes": row["successes"], "failures": row["failures"],
            "last_http_status": row["last_http_status"], "last_error": row["last_error"],
            "remaining": row["remaining"] if row["remaining_exact"] else None,
            "remaining_exact": bool(row["remaining_exact"]),
            "estimate": est, "reset_at": row["reset_at"], "last_used": row["last_used"],
        })
    overall = "OK"
    if configured == 0:
        overall = "NO_PROVIDERS_CONFIGURED"
    elif limited == configured:
        overall = "ALL_PROVIDERS_LIMIT_REACHED"
    return {"overall": overall, "providers": rows}


class SearchManager:
    """Runs one query through the first eligible provider, falling back safely."""

    def __init__(self, providers=None):
        self.providers = providers if providers is not None else build_providers()

    def has_budget(self):
        return any(is_eligible(p) for p in self.providers)

    def search(self, query, count=10):
        if not any(p.configured for p in self.providers):
            raise NoProvidersConfigured("No search provider key found in .env")
        kinds = []
        for p in self.providers:
            if not is_eligible(p):
                continue
            try:
                results = p.search(query, count)
            except ProviderError as err:
                record(p.name, query, err.kind, err.http_status, str(err), error_kind=err.kind,
                       reset_at=err.reset_at)
                kinds.append(err.kind)
                continue  # fall back to the next eligible provider
            record(p.name, query, "ok", 200, quota=p.last_quota)
            return results, p.name
        if kinds and any(k != "limit" for k in kinds):
            # We reached providers, none succeeded, and at least one failure was not a confirmed quota limit.
            raise ProvidersFailed("Every eligible provider returned an error")
        raise AllProvidersExhausted("No eligible provider is available")

    def recheck(self, name):
        """Manual recheck: one tiny request that either confirms the provider works or the limit."""
        for p in self.providers:
            if p.name != name:
                continue
            if not p.configured:
                raise NoProvidersConfigured(f"{name} has no key in .env")
            try:
                p.search("test", 1)
            except ProviderError as err:
                record(p.name, "recheck", err.kind, err.http_status, str(err), error_kind=err.kind,
                       reset_at=err.reset_at)
            else:
                record(p.name, "recheck", "ok", 200, quota=p.last_quota)
            return
        raise KeyError(name)
