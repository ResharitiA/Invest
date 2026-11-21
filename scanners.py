import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "git_org/training-main/vulnerability-database/vulnerabilities.json"
SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]


def finding(category, path, line, kind, description, remediation, severity="HIGH", **extra):
    identity = f"{category}:{path}:{line}:{kind}:{extra.get('advisoryId', '')}"
    return dict(id=hashlib.sha256(identity.encode()).hexdigest()[:16],
                category=category, path=path, line=line, **{"class": kind},
                severity=severity, description=description, remediation=remediation,
                confidence=0.8, **extra)


def texts(root):
    result = {}
    total = 0
    for path in Path(root).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if any(p in {".git", "node_modules", ".gradle", "build", "__pycache__"} for p in rel.parts):
            continue
        if path.suffix.lower() not in {".kt", ".kts", ".yaml", ".yml", ".properties", ".toml", ".lockfile", ".json", ".xml", ".env"} and path.name != "gradle.lockfile":
            continue
        size = path.stat().st_size
        total += size
        if size > 1024 * 1024 or total > 30 * 1024 * 1024:
            raise ValueError("Превышен лимит исходных файлов")
        result[rel.as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return result


def secrets(files):
    out = []
    for path, text in files.items():
        lines = text.splitlines()
        for n, line in enumerate(lines, 1):
            if line.strip().startswith(("//", "#", "*")):
                continue
            role = re.search(r"(?i)(password|passwd|api[_-]?key|token|authorization|private[_-]?key)", line)
            if not role:
                continue
            values = re.findall(r'["\x27]([^"\x27]+)["\x27]', line)
            if path.endswith((".yml", ".yaml", ".properties", ".env")):
                values += [line.split(":", 1)[-1].strip()]
            candidates = [v for v in values if len(v) >= 8 and not re.search(r"(?i)(\$\{|getenv|placeholder|example|changeme|your[_-]|^[A-Z_]+$)", v)]
            if not candidates:
                continue
            kind = "password" if re.search("(?i)pass", role[0]) else "api-key" if re.search("(?i)api", role[0]) else "configuration-token"
            if "?:" in line:
                kind = "secret-fallback"
            out.append(finding("SECRET", path, n, kind, "В исходниках хранится встроенный секрет; значение скрыто.", "Удалить литерал, отозвать секрет и читать новое значение из окружения.", "HIGH"))
        for match in re.finditer(r'(?m)^\s*(?:val|var)\s+(\w+)\s*=\s*"([^"\n]+)"', text):
            name, value = match.groups()
            tail = text[match.end():match.end()+700]
            if len(value) >= 6 and re.search(r'(?i)(Authorization|Bearer)', tail) and re.search(r'\b' + re.escape(name) + r'\s*\+', tail):
                n = text.count("\n", 0, match.start()) + 1
                out.append(finding("SECRET", path, n, "recoverable-token", "Токен восстанавливается из строковых констант.", "Получать цельный токен из хранилища секретов.", "MEDIUM"))
    return out


def version(value):
    parts = re.findall(r"\d+|[A-Za-z]+", value.lower())
    nums = []
    suffix = []
    for p in parts:
        if p.isdigit() and not suffix:
            nums.append(int(p))
        else:
            suffix.append(p)
    return tuple((nums + [0] * 12)[:12]), (1 if not suffix or suffix == ["final"] else 0), tuple(suffix)


def affected(value, bounds):
    v = version(value)
    lo, hi = bounds.get("lower"), bounds.get("upper")
    return (not lo or v > version(lo) or (v == version(lo) and bounds.get("lowerInclusive", True))) and (not hi or v < version(hi) or (v == version(hi) and bounds.get("upperInclusive", False)))


def sca(files):
    database = json.loads(DATABASE.read_text(encoding="utf-8"))["vulnerabilities"]
    variables = {}
    for path, text in files.items():
        if path.endswith(".properties"):
            variables.update(re.findall(r"(?m)^\s*([\w.]+)\s*=\s*([^\s#]+)", text))
    for text in files.values():
        variables.update(re.findall(r'val\s+(\w+)\s*=\s*"([^"]+)"', text))
        for name, prop in re.findall(r'val\s+(\w+)\s*=\s*providers.gradleProperty\("([^"]+)"\).get\(\)', text):
            if prop in variables:
                variables[name] = variables[prop]
    locks = {}
    for path, text in files.items():
        if path.endswith("gradle.lockfile"):
            for group, artifact, value in re.findall(r"(?m)^([^:#\s]+):([^:\s]+):([^=\s]+)=", text):
                locks[(group, artifact)] = value
    out = []
    for path, text in files.items():
        if not path.endswith((".gradle.kts", ".gradle")):
            continue
        for match in re.finditer(r'"([\w.\-]+):([\w.\-]+):([^"\s]+)"', text):
            before = text[:match.start()]
            if "dependencies" not in before or re.search(r"//[^\n]*$", before):
                continue
            group, artifact, value = match.groups()
            value = re.sub(r"\$\{?(\w+)\}?", lambda m: variables.get(m[1], m[0]), value)
            value = locks.get((group, artifact), value)
            if "$" in value:
                continue
            for advisory in database:
                if (group, artifact) != (advisory["group"], advisory["artifact"]):
                    continue
                if not any(affected(value, r) for r in advisory["ranges"]):
                    continue
                fixes = advisory.get("fixedVersions", [])
                out.append(finding("SCA", path, text.count("\n", 0, match.start())+1, "known-vulnerable-dependency",
                    f"Известная уязвимость зависимости {group}:{artifact}:{value}.",
                    "Исправленные версии по справочнику: " + (", ".join(fixes) or "не указаны"),
                    advisory.get("severity", "MEDIUM"), group=group, artifact=artifact, version=value,
                    advisoryId=advisory["id"], aliases=advisory.get("aliases", []),
                    ranges=advisory["ranges"], fixedVersions=fixes, sources=advisory.get("sources", [])))
    return out


def sast(files):
    rules = [
        ("tls-verification", r"HostnameVerifier\s*\{[^}]*->\s*true\s*\}", "Отключена проверка имени TLS-сервера.", "Использовать стандартную проверку TLS.", "MEDIUM"),
        ("weak-cryptography", r'(?:MessageDigest|Cipher)\.getInstance\(\s*"(?:MD5|SHA-?1|DES|RC4)"', "Используется слабый криптографический алгоритм.", "Для паролей использовать Argon2/bcrypt, для дайджеста SHA-256.", "MEDIUM"),
        ("command-injection", r'(?:Runtime\.getRuntime\(\)\.exec|ProcessBuilder)\([^\n]*\$', "Динамическое построение команды ОС.", "Не передавать пользовательские данные командному интерпретатору.", "HIGH"),
    ]
    out = []
    for path, text in files.items():
        if not path.endswith(".kt"):
            continue
        for kind, pattern, desc, fix, sev in rules:
            for m in re.finditer(pattern, text):
                out.append(finding("SAST", path, text.count("\n", 0, m.start())+1, kind, desc, fix, sev))
        for m in re.finditer(r'(?m)^.*(?:val|var)\s+(\w+)\s*=\s*".*(?:SELECT|UPDATE|DELETE|INSERT).*\$.*$', text, re.I):
            if re.search(r"\b(?:query\w*|execute|update)\s*\(\s*" + re.escape(m[1]), text[m.end():m.end()+600]):
                out.append(finding("SAST", path, text.count("\n", 0, m.start())+1, "sql-injection", "Интерполяция данных в SQL перед выполнением JDBC.", "Использовать параметризованный SQL и передавать значения отдельно."))
        if re.search(r'allowedOrigin(?:Patterns|s).*"\*"', text) and re.search(r"allowCredentials\s*=\s*true", text):
            m = re.search(r"allowedOrigin", text)
            out.append(finding("SAST", path, text.count("\n", 0, m.start())+1, "credentialed-cors", "Произвольный origin разрешён вместе с credentials.", "Указать точный список доверенных origin.", "MEDIUM"))
        for m in re.finditer(r'requestMatchers\([^\n]+\)\.permitAll\(\)', text):
            if re.search(r'(?i)(export|admin|/\*\*)', m[0]):
                out.append(finding("SAST", path, text.count("\n", 0, m.start())+1, "route-authorization", "Чувствительный маршрут доступен без проверки роли.", "Ограничить маршрут необходимой ролью; проверить контроллер."))
    return out


SCANNERS = {"secrets": secrets, "sca": sca, "sast": sast}


def scan(root, selected=None):
    files = texts(root)
    findings = [f for name in (selected or SCANNERS) for f in SCANNERS[name](files)]
    unique = {f["id"]: f for f in findings}
    return sorted(unique.values(), key=lambda f: SEVERITIES.index(f["severity"]))
