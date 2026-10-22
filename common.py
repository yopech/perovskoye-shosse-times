"""Local state only. No paid inference APIs and no credentials in logs."""
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT
STATE = ROOT / "data" / "private"
ISSUES = ROOT / "output" / "issues"


def initialize():
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    ISSUES.mkdir(parents=True, exist_ok=True)


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".gazette-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def config():
    return read_json(ROOT / "config.json") or read_json(ROOT / "config.example.json")


def now():
    return datetime.now(ZoneInfo(config()["timezone"]))


def day_dir(day=None):
    day = day or now().date().isoformat()
    datetime.strptime(day, "%Y-%m-%d")
    path = STATE / day
    path.mkdir(parents=True, exist_ok=True)
    return path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def seal(packet):
    packet.pop("packet_sha256", None)
    packet["packet_sha256"] = digest(packet)
    return packet


def secret(key):
    return read_json(STATE / "credentials.json", {}).get(key)


def set_secret(key, value):
    data = read_json(STATE / "credentials.json", {})
    data[key] = value
    write_json(STATE / "credentials.json", data)


def source_status(state, detail):
    return {"state": state, "detail": detail, "checked_at": now().isoformat()}


def section_name(key):
    defaults = {"agenda":"Сегодняшние делишки", "mail":"Разгребаем почту", "chats":"Пока ты не читал чаты",
        "news":"Главное в России и Мире", "events":"Куда сходить с кайфом", "meme":"Щитпост дня"}
    return config().get("section_titles",{}).get(key,defaults[key])


def mail_groups(articles, sources):
    groups = {"corporate":[], "personal":[]}
    for article in articles:
        accounts = {sources[sid].get("account","corporate") for sid in article["source_ids"]}
        if len(accounts)!=1 or not accounts.issubset(groups):
            raise ValueError("Заметка смешивает корпочту и личную почту")
        groups[accounts.pop()].append(article)
    return [(key,label,groups[key]) for key,label in (("corporate","Корпочта"),("personal","Личная")) if groups[key]]
