import asyncio
import json
from contextlib import closing
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app
from backend import scheduling as s


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB", tmp_path / "garden.db")
    monkeypatch.setenv("GARDEN_DOMINO_TOKEN", "test-token-" + "a" * 40)
    with TestClient(app.app) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token-" + "a" * 40}


def flow(client, **options):
    value = app.default_flow()
    value["nodes"][0].update(type="schedule", config={"mode": "interval", "minutes": 1, "enabled": True, **options})
    r = client.put("/api/workflows/default", json=value)
    assert r.status_code == 200, r.text
    return r.json()


def due():
    with closing(app.connection()) as c, c:
        c.execute("UPDATE schedules SET next_at=next_at-600")


def test_auth_and_results(client):
    assert client.post("/api/domino/tick").status_code == 401
    assert client.get("/api/domino/schedules", headers=AUTH).status_code == 200
    assert client.get("/api/domino/runs/missing", headers=AUTH).status_code == 404


def test_dispatch_real_scan_and_duplicate(client):
    flow(client)
    due()
    r = client.post("/api/domino/tick", headers=AUTH)
    d = r.json()["dispatches"]
    assert len(d) == 1 and d[0]["status"] == "accepted", r.text
    ident = d[0]["run_id"]
    for _ in range(100):
        run = client.get("/api/domino/runs/" + ident, headers=AUTH).json()
        if run["status"] != "running":
            break
        import time
        time.sleep(.02)
    assert run["status"] == "success", run
    assert run["findings"]
    assert client.post("/api/domino/tick", headers=AUTH).json()["dispatches"] == []
    assert len(client.get("/api/runs").json()) == 1


def test_disabled_and_invalid(client):
    flow(client, enabled=False)
    assert client.post("/api/domino/tick", headers=AUTH).json()["dispatches"] == []
    value = app.default_flow()
    value["nodes"][0].update(type="schedule", config={"timezone": "invalid/zone"})
    assert client.put("/api/workflows/default", json=value).status_code == 422
    value["nodes"][0]["config"] = {"mode": "cron", "cron": "bad"}
    assert client.put("/api/workflows/default", json=value).status_code == 422
    value["nodes"][0]["config"] = {"mode": "interval", "minutes": 0}
    assert client.put("/api/workflows/default", json=value).status_code == 422


def test_overlap_is_recorded_and_skip_is_not_retried(client):
    f = flow(client)
    run = app.new_run(f, app.get("projects", "demo"))
    app.save("runs", run)
    due()
    d = client.post("/api/domino/tick", headers=AUTH).json()["dispatches"]
    assert d[0]["status"] == "skipped"
    assert client.post("/api/domino/tick", headers=AUTH).json()["dispatches"] == []
    assert len(client.get("/api/domino/dispatches", headers=AUTH).json()) == 1


def test_save_does_not_reset_schedule_and_change_resets(client):
    f = flow(client)
    before = client.get("/api/domino/schedules", headers=AUTH).json()["schedules"][0]["next_at"]
    f["name"] = "Renamed"
    assert client.put("/api/workflows/default", json=f).status_code == 200
    assert client.get("/api/domino/schedules", headers=AUTH).json()["schedules"][0]["next_at"] == before
    f["nodes"][0]["config"]["enabled"] = False
    client.put("/api/workflows/default", json=f)
    assert client.get("/api/domino/schedules", headers=AUTH).json()["schedules"][0]["next_at"] is None


@pytest.mark.parametrize("trigger", [None, "schedule"])
def test_recovery_preserves_no_replay(client, trigger):
    f = flow(client)
    run = app.new_run(f, app.get("projects", "demo"))
    run["trigger"] = trigger
    run["finished"] = run["started"] + 1
    app.save("runs", run)
    s.recover(app)
    restored = app.get("runs", run["id"])
    assert restored["status"] == "failed"
    assert restored["finished"] == run["finished"]


def test_finished_running_record_does_not_block(client):
    f = flow(client)
    run = app.new_run(f, app.get("projects", "demo"))
    run["finished"] = run["started"] + 1
    app.save("runs", run)
    due()
    result = client.post("/api/domino/tick", headers=AUTH).json()
    assert result["dispatches"][0]["status"] == "accepted"


def test_timezone_cron_and_daily():
    now = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    daily = s.config({"time": "09:00", "timezone": "Europe/Moscow"})
    assert s.following(daily, now) == now + 6 * 3600
    cron = s.config({"mode": "cron", "cron": "*/5 * * * *", "timezone": "UTC"})
    assert s.following(cron, now) == now + 300
    assert s.following(s.config({"mode": "interval", "minutes": 2}), now) == now + 120
