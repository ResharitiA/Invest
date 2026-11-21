import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app
import notifications as n


@pytest.fixture
def receiver(monkeypatch):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(503 if self.path == "/fail" else 302 if self.path == "/redirect" else 204)
            self.send_header("Location", "http://127.0.0.1/private")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = socket.create_connection
    monkeypatch.setattr(n, "addresses", lambda host, port: ["127.0.0.1"])
    def connect(address, *args, **kwargs):
        if address == ("127.0.0.1", 80):
            address = server.server_address
        return original(address, *args, **kwargs)
    monkeypatch.setattr(socket, "create_connection", connect)
    yield received
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:8.8.8.8", "224.0.0.1", "ff02::1"])
def test_ssrf(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", (address, 80))])
    with pytest.raises(ValueError):
        n.addresses("example.test", 80)


@pytest.mark.parametrize("config", [
    {"url": "file:///etc/passwd"}, {"url": "http://user:pass@example.test"},
    {"url": "https://example.test?token=secret"}, {"url": "http://example.test", "token": "secret"},
    {"url": "https://example.test", "headers": {"Host": "evil"}},
    {"url": "https://example.test", "headers": {"X-Test": "a\r\nb"}},
    {"url": "http://example.test:9000"}, {"url": "https://example.test", "timeout": 31},
])
def test_invalid_config(config):
    with pytest.raises(ValueError):
        n.validate(config)


def test_credentials(monkeypatch):
    monkeypatch.setenv("GARDEN_WEBHOOK_KEY", Fernet.generate_key().decode())
    stored = n.protect({"url": "https://example.test", "token": "synthetic-token", "headers": {"X-Key": "synthetic-header"}})
    assert "synthetic" not in json.dumps(stored)
    assert "_credentials" not in n.public(stored)
    assert n.public(stored)["has_credentials"]
    assert n.protect(n.public(stored), stored)["_credentials"] == stored["_credentials"]
    assert "_credentials" not in n.protect({**n.public(stored), "clear_credentials": True}, stored)
    with pytest.raises(ValueError):
        n.protect({**n.public(stored), "url": "http://example.test"}, stored)


def test_delivery(receiver):
    for path, expected in [("/", "success"), ("/fail", "failed"), ("/redirect", "failed")]:
        result = n.send({"url": "http://receiver.test" + path}, {"test": True})
        assert result["status"] == expected
    assert len(receiver) == 3


def test_timeout(monkeypatch):
    monkeypatch.setattr(n, "addresses", lambda *a: ["8.8.8.8"])
    def timeout(*a, **k):
        raise TimeoutError("sensitive internal details")
    monkeypatch.setattr(socket, "create_connection", timeout)
    result = n.send({"url": "https://example.test"}, {})
    assert result["status"] == "failed"
    assert "sensitive" not in json.dumps(result)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB", tmp_path / "test.sqlite3")
    with TestClient(app.app) as client:
        yield client


def flow():
    return {"name": "Webhook test", "project_id": "demo",
            "nodes": [{"id": "m", "type": "manual"}, {"id": "w", "type": "webhook",
                       "config": {"url": "http://receiver.test"}}],
            "edges": [{"source": "m", "target": "w"}]}


def test_workflow(client, receiver):
    saved = client.post("/api/workflows", json=flow())
    assert saved.status_code == 200, saved.text
    ident = saved.json()["id"]
    assert client.post("/api/notifications/test", json={"workflow_id": ident, "node_id": "w"}).json()["status"] == "success"
    run = client.post(f"/api/workflows/{ident}/run").json()
    for _ in range(100):
        result = client.get("/api/runs/" + run["id"]).json()
        if result["status"] != "running":
            break
        time.sleep(.02)
    assert result["status"] == "success", result
    assert result["notifications"]["w"]["http_status"] == 204
    assert receiver[-1]["run_id"] == run["id"]
    assert receiver[-1]["total_findings"] == 0
    assert receiver[0]["test"] is True


@pytest.mark.parametrize("skip", [False, True])
def test_failed_or_skipped_delivery(client, receiver, skip):
    body = flow()
    body["nodes"][1]["config"]["url"] += "/fail"
    if skip:
        body["nodes"][0].update(type="if", config={"minimum": 1})
        body["edges"][0]["branch"] = "true"
    saved = client.post("/api/workflows", json=body).json()
    run = {"id": "isolated-run", "status": "running", "source": "organizer",
           "nodes": {"m": "waiting", "w": "waiting"}, "findings": []}
    asyncio.run(app.execute(run, app.get("workflows", saved["id"])))
    assert run["status"] == ("success" if skip else "failed")
    assert run["nodes"]["w"] == ("skipped" if skip else "failed")
    assert len(receiver) == (0 if skip else 1)


def test_api_secrets(client, monkeypatch):
    monkeypatch.setenv("GARDEN_WEBHOOK_KEY", Fernet.generate_key().decode())
    body = flow()
    body["nodes"][1]["config"] = {"url": "https://receiver.test", "token": "synthetic-secret"}
    response = client.post("/api/workflows", json=body)
    assert response.status_code == 200
    assert "synthetic-secret" not in response.text
    assert "synthetic-secret" not in client.get("/api/workflows").text
    assert "synthetic-secret" not in app.DB.read_bytes().decode(errors="ignore")
    saved = response.json()
    assert client.put("/api/workflows/" + saved["id"], json=saved).status_code == 200


def test_terminal_validation(client):
    body = flow()
    body["edges"].append({"source": "w", "target": "m"})
    assert client.post("/api/workflows", json=body).status_code == 422


def test_browser(tmp_path, monkeypatch, receiver):
    import uvicorn
    from playwright.sync_api import sync_playwright
    monkeypatch.setattr(app, "DB", tmp_path / "browser.sqlite3")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app.app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}")
            page.locator("#projectSelect").wait_for(state="attached")
            page.locator('button[data-page="editor"]').click()
            page.locator("#nodeType").select_option("webhook")
            page.get_by_role("button", name="Добавить ноду", exact=True).click()
            page.locator('.node').last.click()
            page.locator('#nodeSettings input[type="url"]').fill("http://receiver.test")
            page.evaluate("""() => {const w=workflow.nodes.at(-1);workflow.edges.push({source:workflow.nodes.at(-2).id,target:w.id,branch:''})}""")
            page.get_by_role("button", name="Сохранить и проверить подключение", exact=True).click()
            page.wait_for_function("document.querySelector('#editorNotice').textContent.includes('Тест Webhook: success')")
            page.get_by_role("button", name="Run workflow", exact=True).click()
            page.wait_for_function("document.querySelector('#state').textContent === 'success'")
            page.locator('button[data-page="history"]').click()
            page.wait_for_function("document.querySelector('#historyList').textContent.includes('HTTP 204')")
            page.reload()
            page.locator("#projectSelect").wait_for(state="attached")
            assert page.locator(".node").count() == 7
            assert not errors, errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
