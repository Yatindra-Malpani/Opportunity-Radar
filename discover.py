"""The discovery pipeline: goals -> search -> classify -> learn sources -> enrich -> store.

No site list exists anywhere in this code. Sources enter the `sources` table only
because a search provider returned them, and they stay active only while they keep
producing relevant items.
"""
import os
import threading
from collections import namedtuple
from datetime import date

import db
from classify import classify, match_city
from dates import parse_date
from extract import analyse_page, parse_feed
from fetch import Fetcher
from providers import AllProvidersExhausted, NoProvidersConfigured, ProvidersFailed, SearchManager
from queries import build_pool, pick_queries
from urlnorm import domain_of, normalize_url

Candidate = namedtuple("Candidate", "norm_url url domain category score title")

STATE = {"running": False, "phase": "idle", "message": "", "queries_done": 0, "queries_total": 0,
         "pages_fetched": 0, "items_seen": 0, "items_new": 0, "outcome": None}
_run_lock = threading.Lock()


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Discovery:
    def __init__(self, manager=None, fetcher=None):
        self.manager = manager or SearchManager()
        self.fetcher = fetcher or Fetcher()
        self.max_queries = _int_env("RADAR_MAX_QUERIES", 16)
        self.max_pages = _int_env("RADAR_MAX_PAGES", 30)
        self.max_listing_links = _int_env("RADAR_MAX_LISTING_LINKS", 40)
        self.cities = db.get_setting("cities") or ["Mumbai", "Pune"]
        self.include_online = bool(db.get_setting("include_online"))
        self.candidates: list[Candidate] = []
        self.run_id = None
        self.listing_budget = self.max_listing_links

    # ------------------------------------------------------------------ helpers
    def _set(self, **kw):
        STATE.update(kw)

    def _store(self, title, url, cls, found_via, published=None, snippet=None, extra=None):
        norm = normalize_url(url)
        dom = domain_of(url)
        if not norm or not title:
            return None
        meta = {"snippet": snippet, "tags": cls.get("tags") or None}
        meta.update(extra or {})
        item = {"norm_url": norm, "url": url, "title": title, "source_name": dom, "domain": dom,
                "category": cls["category"], "city": cls["city"], "published_at": parse_date(published or ""),
                "metadata": {k: v for k, v in meta.items() if v}, "found_via": found_via}
        is_new = db.upsert_item(item)
        STATE["items_seen"] += 1
        STATE["items_new"] += int(is_new)
        db.touch_source(dom, hit=True)
        self.candidates.append(Candidate(norm, url, dom, cls["category"], cls["score"], title))
        return is_new

    # ------------------------------------------------------------------ phase 1
    def poll_learned_feeds(self):
        """Feeds the bot found itself on earlier runs. Costs no search quota."""
        for dom, feeds in db.sources_with_feeds():
            for feed_url in feeds[:2]:
                res = self.fetcher.get(feed_url)
                db.touch_source(dom, fetch_ok=res.ok if not res.skipped else None, robots_blocked=res.robots_blocked)
                if not res.ok:
                    continue
                STATE["pages_fetched"] += 1
                for e in parse_feed(res.text):
                    cls = classify(e["title"], e.get("summary"), e["url"], None, self.cities, self.include_online)
                    if cls:
                        self._store(e["title"], e["url"], cls, f"feed:{dom}", e.get("published_at"), e.get("summary"))
                    else:
                        db.touch_source(dom, hit=False)

    # ------------------------------------------------------------------ phase 2
    def search_phase(self):
        pool = build_pool(self.cities, self.include_online)
        plan = pick_queries(pool, self.max_queries, offset=db.run_count() - 1)
        self._set(queries_total=len(plan), phase="searching")
        for intent, query in plan:
            try:
                results, provider = self.manager.search(query)
            except AllProvidersExhausted:
                return "limit"
            except NoProvidersConfigured:
                return "no_providers"
            except ProvidersFailed:
                return "provider_error"
            STATE["queries_done"] += 1
            for r in results:
                dom = domain_of(r["url"])
                if not dom or db.is_muted(dom):
                    continue
                cls = classify(r["title"], r["snippet"], r["url"], intent, self.cities, self.include_online)
                if not cls:
                    db.touch_source(dom, hit=False)
                    continue
                self._store(r["title"], r["url"], cls, f"search:{provider}", r["published"], r["snippet"],
                            {"query": query, "provider": provider})
        return "ok"

    # ------------------------------------------------------------------ phase 3
    def enrich_phase(self):
        self._set(phase="reading pages")
        seen, queue = set(), sorted(self.candidates, key=lambda c: -c.score)
        for cand in queue:
            if STATE["pages_fetched"] >= self.max_pages:
                break
            if cand.norm_url in seen:
                continue
            seen.add(cand.norm_url)
            res = self.fetcher.get(cand.url)
            db.touch_source(cand.domain, fetch_ok=None if res.skipped and not res.robots_blocked else res.ok,
                            robots_blocked=res.robots_blocked)
            if not res.ok:
                continue
            STATE["pages_fetched"] += 1
            page = analyse_page(res.text, cand.url)
            db.add_feeds(cand.domain, page["feeds"])
            self._apply_page(cand, page)
            self._follow_listing(cand, page)

    def _apply_page(self, cand, page):
        want = "job" if cand.category == "job" else "event"
        fact = next((f for f in page["facts"] if f["kind"] == want), None)
        fields, meta = {}, {}
        if fact:
            fields.update(event_date=fact.get("event_date"), deadline=fact.get("deadline"),
                          published_at=fact.get("published_at"))
            city = match_city(fact.get("city_text") or "", self.cities)
            if not city and fact.get("online") and self.include_online:
                city = "Online"
            fields["city"] = city
            meta.update(organizer=fact.get("organizer"), employment=fact.get("employment"),
                        location=fact.get("city_text"))
        if not fields.get("deadline") and page["text_deadline"] and cand.category != "resource":
            fields["deadline"] = page["text_deadline"]
            meta["deadline_source"] = "found in page text"
        fields.setdefault("published_at", None)
        if not fields.get("published_at"):
            fields["published_at"] = page["published_at"]
        if page["site_name"]:
            fields["source_name"] = page["site_name"]
        db.update_item_fields(cand.norm_url, fields, meta)

    def _follow_listing(self, cand, page):
        """If the page is a listing (many relevant links), harvest its relevant entries too."""
        if self.listing_budget <= 0 or len(page["anchors"]) < 12:
            return
        added = 0
        for href, label in page["anchors"]:
            if added >= 8 or self.listing_budget <= 0:
                break
            if len(label.split()) < 3 or domain_of(href) != cand.domain or normalize_url(href) == cand.norm_url:
                continue
            cls = classify(label, None, href, cand.category, self.cities, self.include_online, min_score=3)
            if cls and cls["category"] == cand.category:
                self._store(label, href, cls, f"listing:{cand.domain}")
                added += 1
                self.listing_budget -= 1

    # ------------------------------------------------------------------ main
    def run(self):
        self.run_id = db.start_run()
        self._set(running=True, phase="starting", message="", queries_done=0, queries_total=0,
                  pages_fetched=0, items_seen=0, items_new=0, outcome=None)
        outcome, message = "ok", ""
        try:
            self._set(phase="checking learned feeds")
            self.poll_learned_feeds()
            outcome = self.search_phase()
            self.enrich_phase()
            muted = db.prune_sources()
            if outcome == "limit":
                message = "ALL PROVIDERS LIMIT REACHED. Search stopped; learned feeds were still checked."
            elif outcome == "provider_error":
                message = ("Search providers returned errors (not a quota limit), so searching stopped. "
                           "Check the ERROR rows under API Usage; temporary problems usually clear on the next refresh.")
            elif outcome == "no_providers":
                message = "No search provider key found in .env. Add at least one (see README)."
            elif STATE["items_seen"] == 0:
                message = "Search worked but nothing relevant came back this time. Try Refresh again later."
            else:
                message = f"Found {STATE['items_seen']} items ({STATE['items_new']} new)."
            if muted:
                message += f" Muted {muted} noisy source(s)."
        except Exception as exc:  # surface real problems instead of hiding them
            outcome, message = "error", f"Discovery failed: {type(exc).__name__}: {exc}"
        finally:
            db.update_run(self.run_id, finished=db.now_iso(), state=outcome,
                          queries_run=STATE["queries_done"], pages_fetched=STATE["pages_fetched"],
                          items_seen=STATE["items_seen"], items_new=STATE["items_new"], message=message)
            self._set(running=False, phase="idle", message=message, outcome=outcome)
        return {"outcome": outcome, "message": message, **{k: STATE[k] for k in
                ("queries_done", "pages_fetched", "items_seen", "items_new")}}


def start_background(manager=None, fetcher=None):
    """Start a refresh in a thread. Returns False if one is already running."""
    if not _run_lock.acquire(blocking=False):
        return False
    STATE.update(running=True, phase="starting", message="", outcome=None)

    def _job():
        try:
            Discovery(manager, fetcher).run()
        finally:
            _run_lock.release()

    threading.Thread(target=_job, daemon=True).start()
    return True
