import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field


class Webhook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)
    method: str = "POST"
    timeout: int = Field(default=10, ge=1, le=30)
    message: str = Field(default="", max_length=2000)
    token: str = Field(default="", max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    clear_credentials: bool = False
    has_credentials: bool = False


def validate(config):
    c = Webhook.model_validate(config).model_dump()
    u = urlsplit(c["url"])
    if u.scheme not in {"https", "http"} or not u.hostname or u.username or u.password or u.fragment:
        raise ValueError("Webhook: требуется HTTP(S) URL без пароля и фрагмента")
    if u.query:
        raise ValueError("Webhook: секреты и параметры в URL запрещены; используйте заголовки")
    if c["method"] not in {"POST", "PUT"}:
        raise ValueError("Webhook: поддерживаются POST и PUT")
    if u.port not in {None, 80, 443}:
        raise ValueError("Webhook: разрешены только порты 80 и 443")
    if len(c["headers"]) > 20:
        raise ValueError("Слишком много заголовков")
    for k, v in c["headers"].items():
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", k) or k.lower() in {
            "host", "content-length", "transfer-encoding", "connection", "content-type", "authorization"
        } or any(ord(x) < 32 or ord(x) > 126 for x in v) or len(v) > 4096:
            raise ValueError("Webhook: недопустимый заголовок; авторизацию задайте токеном")
    if any(ord(x) < 33 or ord(x) > 126 for x in c["token"]):
        raise ValueError("Webhook: недопустимый токен")
    if u.scheme != "https" and (c["token"] or c["headers"]):
        raise ValueError("Webhook: заголовки и токен требуют HTTPS")
    return c


def cipher():
    key = os.environ.get("GARDEN_WEBHOOK_KEY", "")
    if not key:
        raise ValueError("Задайте GARDEN_WEBHOOK_KEY — ключ Fernet вне базы данных")
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        raise ValueError("GARDEN_WEBHOOK_KEY должен быть ключом Fernet") from None


def protect(config, previous=None):
    c = validate(config)
    old = (previous or {}).get("_credentials")
    token, headers = c.pop("token"), c.pop("headers")
    clear = c.pop("clear_credentials")
    c.pop("has_credentials")
    if token or headers:
        c["_credentials"] = cipher().encrypt(json.dumps({"token": token, "headers": headers}).encode()).decode()
    elif old and not clear:
        c["_credentials"] = old
    if c.get("_credentials") and urlsplit(c["url"]).scheme != "https":
        raise ValueError("Webhook: сохранённые credentials требуют HTTPS")
    return c


def public(config):
    return {**{k: v for k, v in config.items() if k != "_credentials"},
            "has_credentials": bool(config.get("_credentials"))}


def addresses(host, port):
    try:
        values = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        if not values or any(not ipaddress.ip_address(x).is_global or ipaddress.ip_address(x).is_multicast or
                             getattr(ipaddress.ip_address(x), "ipv4_mapped", None) for x in values):
            raise ValueError()
        return values
    except (OSError, ValueError):
        raise ValueError("Webhook: адрес недоступен или относится к закрытой сети") from None


def send(config, payload):
    started = time.monotonic()
    result = {"status": "failed", "http_status": None}
    conn = None
    try:
        c = validate(public(config))
        u = urlsplit(c["url"])
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        address = addresses(host, port)[0]
        headers = {"Content-Type": "application/json"}
        if config.get("_credentials"):
            credentials = json.loads(cipher().decrypt(config["_credentials"].encode()))
            headers.update(credentials["headers"])
            if credentials["token"]:
                headers["Authorization"] = "Bearer " + credentials["token"]
        conn = http.client.HTTPConnection(host, port, timeout=c["timeout"])
        # Connect to the validated IP while retaining the original hostname for TLS.
        conn.sock = socket.create_connection((address, port), timeout=c["timeout"])
        if u.scheme == "https":
            conn.sock = ssl.create_default_context().wrap_socket(conn.sock, server_hostname=host)
        body = json.dumps({**payload, "message": c["message"]}, ensure_ascii=False).encode()
        conn.request(c["method"], u.path or "/", body=body, headers=headers)
        response = conn.getresponse()
        result["http_status"] = response.status
        result["status"] = "success" if 200 <= response.status < 300 else "failed"
        if result["status"] == "failed":
            result["error"] = "Получатель вернул HTTP " + str(response.status)
    except Exception:
        result["error"] = "Доставка не выполнена: проверьте URL, сеть, TLS и ключ шифрования"
    finally:
        if conn:
            conn.close()
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result


def report(run):
    findings = run.get("findings", [])
    return {
        "project": run.get("project_name"), "project_id": run.get("project_id"),
        "workflow": run.get("name"), "run_id": run["id"], "status": "completed",
        "summary": {level.lower(): sum(f.get("severity") == level for f in findings)
                    for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")},
        "total_findings": len(findings),
        "ai_summary": next((v.get("summary", "") for v in reversed(list(run.get("ai", {}).values())) if isinstance(v, dict)), ""),
        "report_url": "/api/runs/" + run["id"] + "/report",
    }
