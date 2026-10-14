"""Opportunity Radar: Flask app. Run with `python app.py` and open http://127.0.0.1:5000"""
import os

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

import db  # noqa: E402
import discover  # noqa: E402
import providers  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
db.init_db()
manager = providers.SearchManager()


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/items")
def api_items():
    a = request.args
    items = db.list_items(category=a.get("category"), city=a.get("city"), deadline=a.get("deadline"),
                          sort=a.get("sort", "newest"), pinned_only=a.get("pinned") == "1")
    return jsonify({"items": items, "counts": db.counts_by_category(), "cities": db.cities_present()})


@app.post("/api/items/<int:item_id>/pin")
def api_pin(item_id):
    body = request.get_json(silent=True) or {}
    db.set_pinned(item_id, bool(body.get("pinned", True)))
    return jsonify({"ok": True})


@app.post("/api/items/<int:item_id>/dismiss")
def api_dismiss(item_id):
    if not db.dismiss_item(item_id):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/refresh")
def api_refresh():
    if not discover.start_background(manager):
        return jsonify({"error": "A refresh is already running"}), 409
    return jsonify({"started": True})


@app.get("/api/refresh/status")
def api_refresh_status():
    return jsonify({**discover.STATE, "last_run": db.last_run()})


@app.get("/api/usage")
def api_usage():
    snap = providers.snapshot(manager.providers)
    snap["sources_learned"] = len(db.list_sources(1000))
    return jsonify(snap)


@app.post("/api/usage/<name>/recheck")
def api_recheck(name):
    try:
        manager.recheck(name)
    except KeyError:
        return jsonify({"error": "Unknown provider"}), 404
    except providers.NoProvidersConfigured as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@app.get("/api/sources")
def api_sources():
    return jsonify({"sources": db.list_sources()})


@app.get("/api/settings")
def api_get_settings():
    return jsonify({"cities": db.get_setting("cities"), "include_online": db.get_setting("include_online")})


@app.post("/api/settings")
def api_set_settings():
    body = request.get_json(silent=True) or {}
    if "cities" in body:
        cities = [c.strip().title() for c in body["cities"] if isinstance(c, str) and c.strip()][:6]
        if not cities:
            return jsonify({"error": "Pick at least one city"}), 400
        db.set_setting("cities", cities)
    if "include_online" in body:
        db.set_setting("include_online", bool(body["include_online"]))
    return api_get_settings()


@app.post("/api/backup")
def api_backup():
    return jsonify({"path": db.backup()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)