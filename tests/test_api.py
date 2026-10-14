import pytest

import db


@pytest.fixture
def client():
    import app as appmod
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def seed():
    base = {"url": "https://a.com/1", "title": "Pune AI Hackathon", "category": "hackathon", "domain": "a.com",
            "city": "Pune", "deadline": "2099-01-01", "metadata": {"snippet": "hello"}}
    db.upsert_item({**base, "norm_url": "https://a.com/1"})
    db.upsert_item({**base, "norm_url": "https://a.com/2", "url": "https://a.com/2", "category": "job", "city": None,
                    "title": "ML intern", "deadline": None})


def test_items_filters_pin_and_counts(client):
    seed()
    d = client.get("/api/items").get_json()
    assert len(d["items"]) == 2 and d["counts"]["hackathon"] == 1 and d["cities"] == ["Pune"]
    assert len(client.get("/api/items?category=job").get_json()["items"]) == 1
    assert len(client.get("/api/items?city=Pune").get_json()["items"]) == 1
    assert len(client.get("/api/items?deadline=none").get_json()["items"]) == 1
    item_id = d["items"][0]["id"]
    assert client.post(f"/api/items/{item_id}/pin", json={"pinned": True}).status_code == 200
    assert len(client.get("/api/items?pinned=1").get_json()["items"]) == 1
    client.post(f"/api/items/{item_id}/pin", json={"pinned": False})
    assert client.get("/api/items?pinned=1").get_json()["items"] == []
    assert len(client.get("/api/items").get_json()["items"]) == 2


def test_usage_endpoint_without_keys_and_recheck_errors(client):
    u = client.get("/api/usage").get_json()
    assert u["overall"] == "NO_PROVIDERS_CONFIGURED" and len(u["providers"]) == 4
    assert client.post("/api/usage/nope/recheck").status_code == 404
    assert client.post("/api/usage/tavily/recheck").status_code == 400


def test_settings_roundtrip_and_validation(client):
    r = client.post("/api/settings", json={"cities": ["mumbai", " nagpur "], "include_online": False}).get_json()
    assert r["cities"] == ["Mumbai", "Nagpur"] and r["include_online"] is False
    assert client.post("/api/settings", json={"cities": ["", "  "]}).status_code == 400


def test_backup_creates_a_valid_copy(client, tmp_path):
    import sqlite3
    seed()
    path = client.post("/api/backup").get_json()["path"]
    assert path.startswith(str(tmp_path))                      # lands next to the database, in backups/
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2


def test_refresh_endpoint_rejects_parallel_runs(client, monkeypatch):
    import discover
    started = []
    monkeypatch.setattr(discover, "start_background", lambda m=None, f=None: started.append(1) or len(started) == 1)
    assert client.post("/api/refresh").status_code == 200
    assert client.post("/api/refresh").status_code == 409
