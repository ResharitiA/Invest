import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from scanners import ROOT, SCANNERS, texts

DB = ROOT / "garden.sqlite3"
DEMO = ROOT / "git_org/training-main/training-project"


def connection():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT PRIMARY KEY, data TEXT)")
    return c


def save(kind, value):
    with connection() as c:
        c.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?)", (kind, value["id"], json.dumps(value, ensure_ascii=False)))
    return value


def all_of(kind):
    with connection() as c:
        return [json.loads(r[0]) for r in c.execute("SELECT data FROM objects WHERE kind=?", (kind,))]


def get(kind, ident):
    value = next((v for v in all_of(kind) if v["id"] == ident), None)
    if value is None:
        raise HTTPException(404, "Объект не найден")
    return value


class Project(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class Node(BaseModel):
    id: str
    type: str
    x: float = 0
    y: float = 0
    config: dict = Field(default_factory=dict)


class Edge(BaseModel):
    source: str
    target: str
    branch: str = ""


class Workflow(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    project_id: str = "demo"
    nodes: list[Node] = Field(min_length=1, max_length=100)
    edges: list[Edge] = Field(default_factory=list, max_length=300)


TYPES = ["manual", "repository", "secrets", "sca", "sast", "merge", "if", "ai"]


def order(flow):
    nodes = {n["id"]: n for n in flow["nodes"]}
    if len(nodes) != len(flow["nodes"]):
        raise ValueError("ID нод должны быть уникальны")
    for n in nodes.values():
        if n["type"] not in TYPES:
            raise ValueError("Неизвестный тип ноды")
    edges = flow["edges"]
    if any(e["source"] not in nodes or e["target"] not in nodes for e in edges):
        raise ValueError("Соединение с отсутствующей нодой")
    result = []
    while len(result) < len(nodes):
        ready = [i for i in nodes if i not in result and all(e["source"] in result for e in edges if e["target"] == i)]
        if not ready:
            raise ValueError("Граф содержит цикл")
        result.extend(ready)
    return result


def default_flow():
    kinds = ["manual", "repository", "secrets", "sca", "sast", "merge"]
    return dict(id="default", name="Full Security Audit", project_id="demo",
                nodes=[dict(id=str(i), type=t, x=60+i*190, y=160, config={}) for i,t in enumerate(kinds)],
                edges=[dict(source=str(i), target=str(i+1), branch="") for i in range(len(kinds)-1)])


@asynccontextmanager
async def lifespan(app):
    if not all_of("projects"):
        save("projects", dict(id="demo", name="Repair Desk · учебный проект", source="organizer"))
    if not all_of("workflows"):
        save("workflows", default_flow())
    yield


app = FastAPI(title="Garden · Security Workflows", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/api/projects")
def projects():
    return all_of("projects")


@app.post("/api/projects")
def create_project(body: Project):
    return save("projects", dict(id=uuid.uuid4().hex, name=body.name, source="empty"))


@app.get("/api/workflows")
def workflows():
    return all_of("workflows")


@app.get("/api/nodes")
def nodes():
    return TYPES


@app.post("/api/workflows")
def create_workflow(body: Workflow):
    return update_workflow(uuid.uuid4().hex, body)


@app.put("/api/workflows/{ident}")
def update_workflow(ident: str, body: Workflow):
    value = dict(id=ident, **body.model_dump())
    get("projects", value["project_id"])
    try:
        order(value)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return save("workflows", value)


@app.get("/api/runs")
def runs():
    return sorted(all_of("runs"), key=lambda r: r["started"], reverse=True)


@app.get("/api/runs/{ident}")
def run_detail(ident: str):
    return get("runs", ident)


@app.get("/api/runs/{ident}/report")
def report(ident: str):
    return {"findings": get("runs", ident)["findings"]}


async def execute(run, flow):
    outputs = {}
    try:
        project = get("projects", flow["project_id"])
        if project["source"] != "organizer":
            raise ValueError("В проект ещё не загружены исходные файлы")
        files = await asyncio.to_thread(texts, DEMO)
        for ident in order(flow):
            node = next(n for n in flow["nodes"] if n["id"] == ident)
            incoming = [e for e in flow["edges"] if e["target"] == ident]
            active = [e for e in incoming if run["nodes"].get(e["source"]) == "success" and (not e.get("branch") or str(outputs[e["source"]].get("condition", "")).lower() == e["branch"])]
            if incoming and not active:
                run["nodes"][ident] = "skipped"
                save("runs", run)
                continue
            merged = {}
            for edge in active:
                for f in outputs[edge["source"]].get("findings", []):
                    merged[f["id"]] = f
            payload = dict(findings=list(merged.values()))
            run["nodes"][ident] = "running"
            save("runs", run)
            await asyncio.sleep(0)
            kind = node["type"]
            if kind in SCANNERS:
                found = await asyncio.to_thread(SCANNERS[kind], files)
                payload["findings"] = list({f["id"]:f for f in payload["findings"]+found}.values())
            elif kind == "if":
                payload["condition"] = len(payload["findings"]) >= int(node["config"].get("minimum", 1))
            elif kind == "ai":
                payload["ai"] = {
                    "provider": "local-safe-fallback",
                    "summary": f"Проверка завершена: обнаружено {len(payload["findings"])} находок. Подключите LLM_PROVIDER и API-ключ для расширенных объяснений.",
                    "risk_level": next((s for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO") if any(f.get("severity") == s for f in payload["findings"])), "INFO"),
                    "recommendations": [{"finding_id": f["id"], "title": f["class"], "explanation": f["description"], "fix": f["remediation"], "priority": f.get("severity", "INFO")} for f in payload["findings"]]
                }
            outputs[ident] = payload
            run["findings"] = list({f["id"]:f for f in run["findings"]+payload["findings"]}.values())
            run["nodes"][ident] = "success"
            save("runs", run)
        run["status"] = "success"
    except Exception as e:
        run["status"] = "failed"
        run["error"] = str(e)
        for ident, state in run["nodes"].items():
            if state == "running":
                run["nodes"][ident] = "failed"
    finally:
        run["finished"] = time.time()
        save("runs", run)


tasks = set()


@app.post("/api/workflows/{ident}/run")
async def start(ident: str):
    flow = get("workflows", ident)
    order(flow)
    run = dict(id=uuid.uuid4().hex, workflow_id=ident, name=flow["name"], started=time.time(), status="running", nodes={}, findings=[], error=None)
    save("runs", run)
    task = asyncio.create_task(execute(run, flow))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return run


@app.get("/api/runs/{ident}/events")
async def events(ident: str):
    get("runs", ident)
    async def stream():
        previous = ""
        while True:
            current = get("runs", ident)
            data = json.dumps(current, ensure_ascii=False)
            if data != previous:
                yield f"data: {data}\n\n"
                previous = data
            if current["status"] != "running":
                break
            await asyncio.sleep(0.2)
    return StreamingResponse(stream(), media_type="text/event-stream")
