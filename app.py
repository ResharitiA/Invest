import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import io
import stat
import zipfile
from pathlib import PurePosixPath

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.scanners import ROOT, SCANNERS, texts
from backend.ai_helper import assess, configured
from contextlib import closing
import copy
from backend import notifications

DB = ROOT / "garden.sqlite3"
DEMO = ROOT / "git_org/training-main/training-project"


def connection():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT PRIMARY KEY, data TEXT)")
    return c


def save(kind, value):
    with closing(connection()) as c, c:
        c.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?)", (kind, value["id"], json.dumps(value, ensure_ascii=False)))
    return value


def all_of(kind):
    with closing(connection()) as c, c:
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


TYPES = ["manual", "repository", "secrets", "sca", "sast", "merge", "if", "ai", "webhook"]


def public_flow(flow):
    value = copy.deepcopy(flow)
    for node in value["nodes"]:
        if node["type"] == "webhook":
            node["config"] = notifications.public(node["config"])
    return value


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
    seen = set()
    for edge in edges:
        branch = edge.get("branch", "")
        if branch not in {"", "true", "false"}:
            raise ValueError("Неизвестная ветка соединения")
        if branch and nodes[edge["source"]]["type"] != "if":
            raise ValueError("Ветви true/false доступны только для IF")
        key = (edge["source"], edge["target"], branch)
        if key in seen:
            raise ValueError("Соединение уже существует")
        seen.add(key)
    for node in nodes.values():
        if node["type"] == "webhook":
            notifications.validate(notifications.public(node["config"]))
            if any(e["source"] == node["id"] for e in edges):
                raise ValueError("Webhook должен быть конечной нодой")
            if not any(e["target"] == node["id"] for e in edges):
                raise ValueError("Подключите вход Webhook к графу")
        if node["type"] == "if":
            minimum = node.get("config", {}).get("minimum", 1)
            if type(minimum) is not int or minimum < 0:
                raise ValueError("Порог IF должен быть целым неотрицательным числом")
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
    return FileResponse(ROOT / "frontend/index.html")


@app.get("/assets/{name}")
def helper_asset(name: str):
    if name not in {"Helper1.obj", "Helper1.mtl", "helper.js"}:
        raise HTTPException(404)
    return FileResponse(ROOT / ("frontend/helper.js" if name == "helper.js" else "3d/" + name), headers={"Cache-Control": "no-store"})


class HelperQuestion(BaseModel):
    run_id: str
    question: str = Field(min_length=1, max_length=2000)


@app.get("/api/assistant")
def assistant_status():
    return {"configured": configured()}


@app.post("/api/assistant")
async def assistant_answer(body: HelperQuestion):
    run = get("runs", body.run_id)
    if run["status"] == "running":
        raise HTTPException(409, "Дождитесь завершения проверки")
    try:
        result = await assess(run["findings"], body.question, history=run.get("assistant", []))
    except Exception as exc:
        raise HTTPException(502, str(exc) if isinstance(exc, ValueError) else "Не удалось связаться с AI-провайдером")
    run = get("runs", body.run_id)
    run.setdefault("assistant", []).append({"question": body.question, "answer": result})
    save("runs", run)
    return result


@app.get("/api/projects")
def projects():
    return all_of("projects")


@app.post("/api/projects")
def create_project(body: Project):
    return save("projects", dict(id=uuid.uuid4().hex, name=body.name, source="empty"))


@app.get("/api/projects/{ident}")
def project_detail(ident: str):
    return get("projects", ident)


def uploaded_files(data, filename):
    def safe_path(name):
        path = PurePosixPath(name.replace("\\", "/"))
        if not name or path.is_absolute() or any(p == ".." or ":" in p for p in path.parts):
            raise ValueError("Недопустимый путь файла")
        if any(p.lower() in {".git", "node_modules", ".venv", "__pycache__"} for p in path.parts):
            raise ValueError("Служебные каталоги загружать нельзя")
        return path.as_posix()

    def decode(name, content):
        allowed = {".kt", ".kts", ".gradle", ".yaml", ".yml", ".properties", ".toml", ".lockfile", ".json", ".xml", ".env", ".sql"}
        if PurePosixPath(name).suffix.lower() not in allowed and PurePosixPath(name).name not in {".env", "gradle.lockfile"}:
            return None
        return content.decode("utf-8-sig")

    result = {}
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000:
                raise ValueError("В архиве больше 1000 элементов")
            total = 0
            seen = set()
            for entry in entries:
                name = safe_path(entry.filename)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ValueError("Ссылки и специальные файлы запрещены")
                key = name.casefold()
                if key in seen:
                    raise ValueError("Повторяющиеся пути в архиве")
                seen.add(key)
                if entry.is_dir():
                    continue
                total += entry.file_size
                if entry.file_size > 1024 * 1024 or total > 30 * 1024 * 1024:
                    raise ValueError("Лимит: 1 МБ на файл, 30 МБ после распаковки")
                with archive.open(entry) as stream:
                    content = stream.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    raise ValueError("Файл превышает 1 МБ")
                text = decode(name, content)
                if text is not None:
                    result[name] = text
    else:
        name = safe_path(filename)
        if len(data) > 1024 * 1024:
            raise ValueError("Файл превышает 1 МБ")
        text = decode(name, data)
        if text is not None:
            result[name] = text
    if not result:
        raise ValueError("Нет поддерживаемых исходников UTF-8")
    return result


@app.post("/api/projects/upload")
async def upload_project(request: Request, project_id: str, filename: str):
    project = get("projects", project_id)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "Загрузка превышает 10 МБ")
    try:
        files = await asyncio.to_thread(uploaded_files, bytes(data), filename)
    except (ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise HTTPException(422, "Загрузка отклонена: " + str(exc))
    version = uuid.uuid4().hex
    save("sources", dict(id=version, project_id=project_id, files=files))
    project.update(source="upload", source_id=version, file_count=len(files), uploaded=time.time())
    return save("projects", project)


@app.get("/api/workflows")
def workflows():
    return [public_flow(f) for f in all_of("workflows")]


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
        previous = next((f for f in all_of("workflows") if f["id"] == ident), {})
        old_nodes = {n["id"]: n for n in previous.get("nodes", [])}
        for node in value["nodes"]:
            if node["type"] == "webhook":
                node["config"] = notifications.protect(node["config"], old_nodes.get(node["id"], {}).get("config"))
        order(value)
    except ValueError:
        raise HTTPException(422, "Некорректный граф или Webhook. Проверьте URL, метод, таймаут, заголовки и GARDEN_WEBHOOK_KEY.")
    save("workflows", value)
    return public_flow(value)


class NotificationTest(BaseModel):
    workflow_id: str
    node_id: str


@app.post("/api/notifications/test")
async def test_notification(body: NotificationTest):
    flow = get("workflows", body.workflow_id)
    node = next((n for n in flow["nodes"] if n["id"] == body.node_id and n["type"] == "webhook"), None)
    if node is None:
        raise HTTPException(404, "Webhook не найден")
    result = await asyncio.to_thread(notifications.send, node["config"], {"test": True, "workflow": flow["name"]})
    save("notification_tests", dict(id=uuid.uuid4().hex, workflow_id=body.workflow_id, node_id=body.node_id, started=time.time(), **result))
    return result


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
        if run.get("source_id"):
            source = get("sources", run["source_id"])
            if source["project_id"] != flow["project_id"]:
                raise ValueError("Исходники принадлежат другому проекту")
            files = source["files"]
        elif run.get("source") == "organizer":
            files = await asyncio.to_thread(texts, DEMO)
        else:
            raise ValueError("В проект ещё не загружены исходные файлы")
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
                payload["ai"] = await assess(payload["findings"])
                run.setdefault("ai", {})[ident] = payload["ai"]
            outputs[ident] = payload
            run["findings"] = list({f["id"]:f for f in run["findings"]+payload["findings"]}.values())
            run["nodes"][ident] = "waiting" if kind == "webhook" else "success"
            save("runs", run)
        deliveries = run.setdefault("notifications", {})
        for node in flow["nodes"]:
            if node["type"] != "webhook" or node["id"] not in outputs:
                continue
            run["nodes"][node["id"]] = "running"
            save("runs", run)
            delivery = await asyncio.to_thread(notifications.send, node["config"], notifications.report(run))
            deliveries[node["id"]] = delivery
            run["nodes"][node["id"]] = delivery["status"]
            save("runs", run)
        if any(d["status"] == "failed" for d in deliveries.values()):
            raise ValueError("Не удалось доставить Webhook; подробности в журнале доставки")
        run["status"] = "success"
    except Exception as e:
        run["status"] = "failed"
        run["error"] = str(e)
        for ident, state in run["nodes"].items():
            if state == "running":
                run["nodes"][ident] = "failed"
            elif state == "waiting":
                run["nodes"][ident] = "skipped"
    finally:
        run["finished"] = time.time()
        save("runs", run)


tasks = set()


@app.post("/api/workflows/{ident}/run")
async def start(ident: str):
    flow = get("workflows", ident)
    try:
        order(flow)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    project = get("projects", flow["project_id"])
    if project["source"] == "empty":
        raise HTTPException(422, "Сначала загрузите исходники проекта")
    run = dict(id=uuid.uuid4().hex, workflow_id=ident, project_id=project["id"], project_name=project["name"],
               source=project["source"], source_id=project.get("source_id"), name=flow["name"], started=time.time(), status="running",
               nodes={n["id"]: "waiting" for n in flow["nodes"]}, findings=[], error=None)
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
