import hashlib
import hmac
import json
import os
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    mode: str = "daily"
    minutes: int = Field(default=60, ge=1, le=525600)
    time: str = "09:00"
    timezone: str = "Europe/Moscow"
    cron: str = "0 9 * * *"


def config(value):
    try:
        result = Schedule.model_validate(value).model_dump()
        zone = ZoneInfo(result["timezone"])
        if result["mode"] == "daily":
            hour, minute = result["time"].split(":")
            if len(hour) != 2 or len(minute) != 2:
                raise ValueError()
            CronTrigger(hour=int(hour), minute=int(minute), timezone=zone)
        elif result["mode"] == "cron":
            CronTrigger.from_crontab(result["cron"], timezone=zone)
        elif result["mode"] != "interval":
            raise ValueError()
        return result
    except Exception as exc:
        raise ValueError("Расписание: проверьте режим, интервал, время HH:MM, cron и часовой пояс IANA") from exc


def following(value, now):
    if value["mode"] == "interval":
        return now + value["minutes"] * 60
    zone = ZoneInfo(value["timezone"])
    if value["mode"] == "cron":
        trigger = CronTrigger.from_crontab(value["cron"], timezone=zone)
    else:
        hour, minute = map(int, value["time"].split(":"))
        trigger = CronTrigger(hour=hour, minute=minute, timezone=zone)
    result = trigger.get_next_fire_time(None, datetime.fromtimestamp(now, timezone.utc) + timedelta(seconds=1))
    if result is None:
        raise ValueError("Расписание не имеет следующего срабатывания")
    return result.timestamp()


def tables(c):
    c.execute("CREATE TABLE IF NOT EXISTS schedules (workflow_id TEXT PRIMARY KEY, revision TEXT NOT NULL, config TEXT NOT NULL, next_at REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS dispatches (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, due REAL NOT NULL, status TEXT NOT NULL, run_id TEXT, error TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS scheduler_meta (id TEXT PRIMARY KEY, value REAL)")


def sync(c, flow, now=None):
    tables(c)
    now = time.time() if now is None else now
    node = next((n for n in flow["nodes"] if n["type"] == "schedule"), None)
    if node is None:
        c.execute("DELETE FROM schedules WHERE workflow_id=?", (flow["id"],))
        return
    value = config(node["config"])
    signature = json.dumps([node["id"], flow["project_id"], value], sort_keys=True)
    revision = hashlib.sha256(signature.encode()).hexdigest()[:24]
    old = c.execute("SELECT revision FROM schedules WHERE workflow_id=?", (flow["id"],)).fetchone()
    if old and old[0] == revision:
        return
    next_at = following(value, now) if value["enabled"] else None
    c.execute("INSERT OR REPLACE INTO schedules VALUES (?,?,?,?)", (flow["id"], revision, json.dumps(value), next_at))


def authorize(request: Request):
    token = os.environ.get("GARDEN_DOMINO_TOKEN", "")
    if not token:
        from pathlib import Path
        token_path = os.environ.get("GARDEN_DOMINO_TOKEN_FILE")
        if not token_path and os.environ.get("LOCALAPPDATA"):
            token_path = str(Path(os.environ["LOCALAPPDATA"]) / "Garden" / "domino-token.txt")
        if token_path:
            try:
                token = Path(token_path).read_text(encoding="utf-8-sig").strip()
            except OSError:
                pass
    if len(token) < 32:
        raise HTTPException(503, "Garden Domino token is not configured (minimum 32 characters)")
    actual = request.headers.get("authorization", "")
    if not hmac.compare_digest(actual.encode(), ("Bearer " + token).encode()):
        raise HTTPException(401, "Invalid scheduler credentials")


def recover(host):
    with closing(host.connection()) as c, c:
        tables(c)
        for (data,) in c.execute("SELECT data FROM objects WHERE kind='runs'").fetchall():
            run = json.loads(data)
            if run["status"] == "running":
                run.update(status="failed", error="Запуск прерван или не завершён корректно; автоматический повтор отключён", finished=run.get("finished") or time.time())
                run["nodes"] = {k: ("failed" if v == "running" else "skipped" if v == "waiting" else v) for k, v in run["nodes"].items()}
                c.execute("UPDATE objects SET data=? WHERE id=?", (json.dumps(run, ensure_ascii=False), run["id"]))
        for flow in host.all_of("workflows"):
            sync(c, flow)


def install(host):
    app = host.app

    @app.get("/api/domino/schedules", dependencies=[Depends(authorize)])
    def schedules():
        with closing(host.connection()) as c:
            tables(c)
            rows = c.execute("SELECT workflow_id,config,next_at FROM schedules").fetchall()
            heartbeat = c.execute("SELECT value FROM scheduler_meta WHERE id='tick'").fetchone()
        return {"last_tick": heartbeat[0] if heartbeat else None,
                "schedules": [dict(workflow_id=i, config=json.loads(v), next_at=n) for i, v, n in rows]}

    @app.get("/api/domino/dispatches", dependencies=[Depends(authorize)])
    def dispatches():
        with closing(host.connection()) as c:
            tables(c)
            rows = c.execute("SELECT id,workflow_id,due,status,run_id,error FROM dispatches ORDER BY due DESC LIMIT 100").fetchall()
        return [dict(zip(("id", "workflow_id", "due", "status", "run_id", "error"), r)) for r in rows]

    @app.get("/api/domino/runs/{ident}", dependencies=[Depends(authorize)])
    def result(ident: str):
        run = host.get("runs", ident)
        if run.get("trigger") != "schedule":
            raise HTTPException(404, "Scheduled run not found")
        return run

    @app.post("/api/domino/tick", dependencies=[Depends(authorize)])
    async def tick():
        now = time.time()
        pending = []
        outcomes = []
        with closing(host.connection()) as c, c:
            tables(c)
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT OR REPLACE INTO scheduler_meta VALUES ('tick',?)", (now,))
            due = c.execute("SELECT workflow_id,revision,config,next_at FROM schedules WHERE next_at<=? ORDER BY next_at LIMIT 20", (now,)).fetchall()
            for ident, revision, raw, due_at in due:
                value = json.loads(raw)
                key = hashlib.sha256(f"{ident}:{revision}:{due_at}".encode()).hexdigest()
                if c.execute("SELECT 1 FROM dispatches WHERE id=?", (key,)).fetchone():
                    continue
                status, run_id, error = "failed", None, None
                try:
                    flow = json.loads(c.execute("SELECT data FROM objects WHERE kind='workflows' AND id=?", (ident,)).fetchone()[0])
                    host.order(flow)
                    project = json.loads(c.execute("SELECT data FROM objects WHERE kind='projects' AND id=?", (flow["project_id"],)).fetchone()[0])
                    active = any(r.get("workflow_id") == ident and r["status"] == "running" and not r.get("finished")
                                 for (data,) in c.execute("SELECT data FROM objects WHERE kind='runs'")
                                 for r in [json.loads(data)])
                    if active:
                        status, error = "skipped", "Предыдущий запуск workflow ещё выполняется"
                    else:
                        run = host.new_run(flow, project)
                        run.update(trigger="schedule", dispatch_id=key, scheduled_for=due_at)
                        run_id = run["id"]
                        c.execute("INSERT INTO objects VALUES ('runs',?,?)", (run_id, json.dumps(run, ensure_ascii=False)))
                        pending.append((run, flow))
                        status = "accepted"
                except Exception as exc:
                    error = str(exc)
                c.execute("INSERT INTO dispatches VALUES (?,?,?,?,?,?)", (key, ident, due_at, status, run_id, error))
                c.execute("UPDATE schedules SET next_at=? WHERE workflow_id=?", (following(value, now), ident))
                outcomes.append(dict(id=key, workflow_id=ident, status=status, run_id=run_id, error=error))
        for run, flow in pending:
            host.launch(run, flow)
        return {"dispatches": outcomes, "time": now}
