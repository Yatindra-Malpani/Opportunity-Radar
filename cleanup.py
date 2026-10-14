"""Re-check saved items against the current rules and move rejected ones to the deleted list."""
import db
from classify import classify

db.init_db()
cities = db.get_setting("cities") or ["Mumbai", "Pune"]
online = bool(db.get_setting("include_online"))
removed = 0
for it in db.list_items(limit=100000):
    snippet = (it.get("metadata") or {}).get("snippet")
    cls = classify(it["title"], snippet, it["url"], it["category"], cities, online)
    if it["pinned"]:
        continue
    if cls is None or cls["category"] != it["category"]:
        if db.dismiss_item(it["id"]):
            removed += 1
print(f"Removed {removed} rows")