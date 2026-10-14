"""SQLite persistence. Standard library only."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("RADAR_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "radar.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    norm_url TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    source_name TEXT,
    domain TEXT,
    category TEXT NOT NULL,
    city TEXT,
    published_at TEXT,
    deadline TEXT,
    event_date TEXT,
    metadata TEXT,
    found_via TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    pinned INTEGER NOT NULL DEFAULT 0,
    pinned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_pinned ON items(pinned);

-- Sources are LEARNED by the bot, never seeded by hand.
CREATE TABLE IF NOT EXISTS sources (
    domain TEXT PRIMARY KEY,
    first_found TEXT NOT NULL,
    last_checked TEXT,
    feed_urls TEXT NOT NULL DEFAULT '[]',
    hits INTEGER NOT NULL DEFAULT 0,        -- relevant items yielded
    misses INTEGER NOT NULL DEFAULT 0,      -- results rejected as irrelevant
    fetch_ok INTEGER NOT NULL DEFAULT 0,
    fetch_fail INTEGER NOT NULL DEFAULT 0,
    blocked_by_robots INTEGER NOT NULL DEFAULT 0,
    muted INTEGER NOT NULL DEFAULT 0
);

-- Items the user deleted. Their URLs are never added again.
CREATE TABLE IF NOT EXISTS dismissed (
    norm_url TEXT PRIMARY KEY,
    dismissed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_usage (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'UNKNOWN',
    requests INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    last_http_status INTEGER,
    last_error TEXT,
    error_kind TEXT,
    remaining INTEGER,
    remaining_exact INTEGER NOT NULL DEFAULT 0,
    reset_at TEXT,
    est_limit INTEGER,
    last_used TEXT
);

CREATE TABLE IF NOT EXISTS provider_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    at TEXT NOT NULL,
    query TEXT,
    http_status INTEGER,
    outcome TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    finished TEXT,
    state TEXT NOT NULL DEFAULT 'running',
    queries_run INTEGER NOT NULL DEFAULT 0,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
"""

DEFAULT_SETTINGS = {
    "cities": ["Mumbai", "Pune"],
    "include_online": True,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect():
    with _lock:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db():
    with connect() as c:
        c.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))


def get_setting(key):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else DEFAULT_SETTINGS.get(key)


def set_setting(key, value):
    with connect() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def upsert_item(item: dict) -> bool:
    """Insert or refresh an item. Returns True if it was new.

    Existing rows keep their pin state and first_seen. Known fields are only
    filled in, never blanked, and nothing is ever invented. Deleted URLs are skipped.
    """
    now = now_iso()
    with connect() as c:
        if c.execute("SELECT 1 FROM dismissed WHERE norm_url=?", (item["norm_url"],)).fetchone():
            return False
        row = c.execute("SELECT id FROM items WHERE norm_url=?", (item["norm_url"],)).fetchone()
        if row is None:
            c.execute(
                """INSERT INTO items(norm_url, url, title, source_name, domain, category, city,
                       published_at, deadline, event_date, metadata, found_via, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["norm_url"], item["url"], item["title"], item.get("source_name"),
                    item.get("domain"), item["category"], item.get("city"),
                    item.get("published_at"), item.get("deadline"), item.get("event_date"),
                    json.dumps(item.get("metadata") or {}), item.get("found_via"), now, now,
                ),
            )
            return True
        c.execute(
            """UPDATE items SET last_seen=?, seen_count=seen_count+1,
                   city=COALESCE(city, ?), published_at=COALESCE(published_at, ?),
                   deadline=COALESCE(deadline, ?), event_date=COALESCE(event_date, ?)
               WHERE id=?""",
            (now, item.get("city"), item.get("published_at"), item.get("deadline"),
             item.get("event_date"), row["id"]),
        )
        return False


def set_pinned(item_id: int, pinned: bool):
    with connect() as c:
        c.execute(
            "UPDATE items SET pinned=?, pinned_at=? WHERE id=?",
            (1 if pinned else 0, now_iso() if pinned else None, item_id),
        )


def dismiss_item(item_id: int) -> bool:
    """Delete an item and remember its URL so refreshes never add it back."""
    with connect() as c:
        row = c.execute("SELECT norm_url FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            return False
        c.execute("INSERT OR REPLACE INTO dismissed(norm_url, dismissed_at) VALUES (?,?)",
                  (row["norm_url"], now_iso()))
        c.execute("DELETE FROM items WHERE id=?", (item_id,))
    return True


def list_items(category=None, city=None, deadline=None, sort="newest", pinned_only=False, limit=500):
    """Filtering and sorting happen in SQL. Missing dates sort last, never faked."""
    where, args = [], []
    if category and category != "all":
        where.append("category=?"); args.append(category)
    if city and city != "all":
        where.append("city=?"); args.append(city)
    if pinned_only:
        where.append("pinned=1")
    today = datetime.now(timezone.utc).date().isoformat()
    due = "COALESCE(deadline, event_date)"
    if deadline == "has":
        where.append(f"{due} IS NOT NULL")
    elif deadline == "open":
        where.append(f"{due} IS NOT NULL AND {due} >= ?"); args.append(today)
    elif deadline == "week":
        where.append(f"{due} IS NOT NULL AND {due} >= ? AND {due} <= date(?, '+7 day')")
        args += [today, today]
    elif deadline == "none":
        where.append(f"{due} IS NULL")
    elif deadline == "hide_expired":
        where.append(f"({due} IS NULL OR {due} >= ?)"); args.append(today)
    sql = "SELECT * FROM items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if sort == "deadline":
        sql += f" ORDER BY ({due} IS NULL OR {due} < date('now')), {due} ASC, first_seen DESC"
    else:
        sql += " ORDER BY COALESCE(published_at, first_seen) DESC"
    sql += " LIMIT ?"; args.append(limit)
    with connect() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    for r in rows:
        r["metadata"] = json.loads(r["metadata"] or "{}")
        r["pinned"] = bool(r["pinned"])
    return rows


def counts_by_category():
    with connect() as c:
        rows = c.execute("SELECT category, COUNT(*) n FROM items GROUP BY category").fetchall()
        pins = c.execute("SELECT COUNT(*) n FROM items WHERE pinned=1").fetchone()["n"]
    out = {r["category"]: r["n"] for r in rows}
    out["_pinned"] = pins
    return out


def cities_present():
    with connect() as c:
        return [r["city"] for r in c.execute(
            "SELECT DISTINCT city FROM items WHERE city IS NOT NULL ORDER BY city").fetchall()]


# ------------------------------------------------------------- learned sources

def touch_source(domain, hit=None, fetch_ok=None, robots_blocked=False):
    """Record what happened with a domain. Sources are created on first sight; never seeded by hand."""
    if not domain:
        return
    now = now_iso()
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO sources(domain, first_found) VALUES (?, ?)", (domain, now))
        if hit is True:
            c.execute("UPDATE sources SET hits=hits+1 WHERE domain=?", (domain,))
        elif hit is False:
            c.execute("UPDATE sources SET misses=misses+1 WHERE domain=?", (domain,))
        if fetch_ok is True:
            c.execute("UPDATE sources SET fetch_ok=fetch_ok+1, last_checked=? WHERE domain=?", (now, domain))
        elif fetch_ok is False:
            c.execute("UPDATE sources SET fetch_fail=fetch_fail+1, last_checked=? WHERE domain=?", (now, domain))
        if robots_blocked:
            c.execute("UPDATE sources SET blocked_by_robots=1 WHERE domain=?", (domain,))


def add_feeds(domain, feeds):
    if not domain or not feeds:
        return
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO sources(domain, first_found) VALUES (?, ?)", (domain, now_iso()))
        row = c.execute("SELECT feed_urls FROM sources WHERE domain=?", (domain,)).fetchone()
        known = json.loads(row["feed_urls"])
        merged = known + [f for f in feeds if f not in known]
        c.execute("UPDATE sources SET feed_urls=? WHERE domain=?", (json.dumps(merged[:5]), domain))


def is_muted(domain):
    with connect() as c:
        row = c.execute("SELECT muted FROM sources WHERE domain=?", (domain,)).fetchone()
    return bool(row and row["muted"])


def sources_with_feeds():
    with connect() as c:
        rows = c.execute(
            "SELECT domain, feed_urls FROM sources WHERE muted=0 AND blocked_by_robots=0 "
            "AND feed_urls!='[]' AND hits>=1 ORDER BY hits DESC LIMIT 40").fetchall()
    return [(r["domain"], json.loads(r["feed_urls"])) for r in rows]


def prune_sources():
    """Mute sources that only ever produce noise or never load. Returns how many were muted."""
    with connect() as c:
        cur = c.execute(
            "UPDATE sources SET muted=1 WHERE muted=0 AND "
            "((hits=0 AND misses>=8) OR (fetch_ok=0 AND fetch_fail>=5))")
        return cur.rowcount


def list_sources(limit=100):
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM sources ORDER BY muted ASC, hits DESC, first_found DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["feed_urls"] = json.loads(d["feed_urls"])
        out.append(d)
    return out


def update_item_fields(norm_url, fields: dict, metadata: dict = None):
    """Fill in fields learned from the page. Existing values are never overwritten with blanks."""
    allowed = {"city", "published_at", "deadline", "event_date", "source_name"}
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed and v:
            sets.append(f"{k}=COALESCE({k}, ?)" if k != "source_name" else f"{k}=?")
            args.append(v)
    with connect() as c:
        if metadata:
            row = c.execute("SELECT metadata FROM items WHERE norm_url=?", (norm_url,)).fetchone()
            if row:
                merged = json.loads(row["metadata"] or "{}")
                merged.update({k: v for k, v in metadata.items() if v})
                sets.append("metadata=?"); args.append(json.dumps(merged))
        if sets:
            c.execute(f"UPDATE items SET {', '.join(sets)} WHERE norm_url=?", (*args, norm_url))


# ------------------------------------------------------------- discovery runs

def start_run():
    with connect() as c:
        cur = c.execute("INSERT INTO discovery_runs(started) VALUES (?)", (now_iso(),))
        return cur.lastrowid


def update_run(run_id, **cols):
    if not cols:
        return
    with connect() as c:
        c.execute(f"UPDATE discovery_runs SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                  (*cols.values(), run_id))


def run_count():
    with connect() as c:
        return c.execute("SELECT COUNT(*) n FROM discovery_runs").fetchone()["n"]


def last_run():
    with connect() as c:
        row = c.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def backup(dest_dir=None):
    """Consistent copy of the database using SQLite's online backup API."""
    dest_dir = dest_dir or os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "radar-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db")
    with _lock:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close(); src.close()
    return dest