#!/usr/bin/env python3
"""Daily newspaper controller; the model can publish text but cannot choose commands."""
import argparse
import asyncio
from file_lock import fcntl
import getpass
import hashlib
import imaplib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from common import ROOT, STATE, ISSUES, config, day_dir, digest, initialize, now, read_json, seal, secret, set_secret, source_status, write_json, mail_groups, section_name
from sources import calendar_event, collect, ingest_news, import_personal_mail

SECTIONS = {"essentials": 3, "mail": 4, "chats": 3, "news": 2, "events": 2}
AGENT_LABEL = "local.perovskoye-times.print"
AWAKE_LABEL = "local.perovskoye-times.awake"
WINDOWS = os.name == 'nt'


def issue_schema():
    article = {"type": "object", "properties": {
        "headline": {"type": "string", "maxLength": 65},
        "body": {"type": "string", "maxLength": 350},
        "source_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
        "event_key": {"type": "string"}},
        "required": ["headline", "body", "source_ids", "priority", "event_key"], "additionalProperties": False}
    properties = {"date": {"type": "string"}, "packet_sha256": {"type": "string"},
        "demo": {"type": "boolean"}, "editorial_line": {"type": "string", "maxLength": 160}}
    properties.update({k: {"type": "array", "items": article, "maxItems": n} for k,n in SECTIONS.items()})
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def validate_issue(issue, packet):
    expected = {"date", "packet_sha256", "demo", "editorial_line", *SECTIONS}
    if set(issue) != expected or not isinstance(issue["demo"], bool):
        raise ValueError("Неверная структура выпуска")
    if issue["date"] != packet["date"] or issue["packet_sha256"] != packet["packet_sha256"]:
        raise ValueError("Дата или отпечаток пакета не совпадают")
    unsealed = dict(packet)
    unsealed.pop("packet_sha256")
    if digest(unsealed) != packet["packet_sha256"]:
        raise ValueError("Пакет изменён после проверки")
    if not isinstance(issue["editorial_line"], str) or len(issue["editorial_line"]) > 160:
        raise ValueError("Слишком длинная реплика редакции")
    sources = {s["id"]:s for s in packet["sources"]}
    words = len(issue["editorial_line"].split())
    for section, maximum in SECTIONS.items():
        articles = issue[section]
        if not isinstance(articles, list) or len(articles) > maximum:
            raise ValueError(f"Слишком много заметок: {section}")
        for article in articles:
            if set(article) != {"headline", "body", "source_ids", "priority", "event_key"}:
                raise ValueError("Неверная структура заметки")
            if not isinstance(article["headline"], str) or not (1 <= len(article["headline"]) <= 65):
                raise ValueError("Неверный заголовок")
            if not isinstance(article["body"], str) or not (1 <= len(article["body"]) <= 350):
                raise ValueError("Неверная длина заметки")
            if type(article["priority"]) is not int or article["priority"] not in range(1,6):
                raise ValueError("Неверный приоритет")
            if not isinstance(article["event_key"], str):
                raise ValueError("Неверный идентификатор события")
            ids = article["source_ids"]
            if not isinstance(ids, list) or not ids or any(not isinstance(sid, str) or sid not in sources for sid in ids):
                raise ValueError("Заметка ссылается на отсутствующий источник")
            if section == "news" and any(sources[sid]["kind"] != "news" for sid in ids):
                raise ValueError("Новости должны ссылаться на проверенные публикации")
            if section == "mail" and any(sources[sid]["kind"] != "mail" for sid in ids):
                raise ValueError("Почтовая заметка должна ссылаться на письма")
            words += len((article["headline"] + " " + article["body"]).split())
    if any(len(group)>2 for _,_,group in mail_groups(issue["mail"],sources)):
        raise ValueError("Не больше двух заметок на каждый почтовый ящик")
    if words > 430:
        raise ValueError("Слишком много текста для одного листа: сократите выпуск")
    for event in packet["events"]:
        start = event.get("start")
        if not isinstance(start, str):
            raise ValueError("Событие без даты")
        value = datetime.fromisoformat(start.replace("Z", "+00:00")) if len(start) > 10 else date.fromisoformat(start)
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("Событие без часового пояса")


def full_text(issue, packet, omitted):
    lines = [f"# {config()['title']} / {issue['date']}", "", issue["editorial_line"], ""]
    if issue["demo"]:
        lines += ["ДЕМОНСТРАЦИЯ: все события и сообщения вымышлены.", ""]
    lines += ["## " + section_name("agenda"), ""]
    for e in packet["events"]:
        lines += [f"- {e['start']}: {e['title']} | {e.get('location', '')}"]
    lookup = {s["id"]: s for s in packet["sources"]}
    labels = {"essentials": "Не проебать", **{key:section_name(key) for key in ("mail","chats","news","events")}}
    for key, label in labels.items():
        if not issue[key]:
            continue
        lines += ["", "## " + label, ""]
        grouped = [(None,None,issue[key])] if key!="mail" else mail_groups(issue[key],lookup)
        for _, group_label, articles in grouped:
            if group_label:
                lines += ["### " + group_label, ""]
            for a in articles:
                lines += [("#### " if group_label else "### ")+a['headline'], "", a["body"], ""]
                for sid in a["source_ids"]:
                    s = lookup[sid]
                    lines += [f"Источник: {s['title']} {s.get('url','')}"]
    lines += ["", "## Состояние источников", ""]
    lines += [f"- {k}: {v['state']} / {v['detail']}" for k,v in packet["status"].items()]
    lines += ["", "## Предупреждения", ""] + ["- " + w for w in packet.get("warnings", [])]
    if omitted:
        lines += ["", "## Не вошло на бумагу", ""] + ["- " + title for title in omitted]
    return "\n".join(lines) + "\n"


def _publish_unlocked(day, issue_path, demo=False, fallback=False):
    from layout import render
    packet = read_json(day_dir(day) / "packet.json")
    issue = read_json(issue_path)
    if not issue or not packet:
        raise ValueError("Нет выпуска или исходного пакета")
    validate_issue(issue, packet)
    if issue["demo"] != demo:
        raise ValueError("Демонстрацию нельзя опубликовать как настоящий выпуск")
    out = ROOT / "output" if demo else ISSUES / day
    out.mkdir(parents=True, exist_ok=True)
    filename = "газета-пример.pdf" if demo else "газета.pdf"
    temp = out / ("." + filename + ".tmp")
    result = render(issue, packet, temp)
    target = out / filename
    sha = hashlib.sha256(temp.read_bytes()).hexdigest()
    os.replace(temp, target)
    (out / ("газета-пример.md" if demo else "полный-выпуск.md")).write_text(full_text(issue, packet, result["omitted"]),encoding='utf-8')
    if not demo:
        write_json(day_dir(day) / "issue.json", issue)
        write_json(day_dir(day) / "ready.json", {"date":day, "demo":False, "pdf":str(target), "sha256":sha,
            "packet_sha256":packet["packet_sha256"], "created_at":now().isoformat(), "mode":"fallback" if fallback else "edited",
            "meme_id":(packet.get("meme") or {}).get("id"), **result})
        if not fallback and packet["status"].get("telegram", {}).get("state") in ("ok", "partial"):
            write_json(STATE / "last-published.json", {"date":day,"collected_at":packet["collected_at"]})
        history = read_json(STATE / "event-history.json", [])
        history.extend(a["event_key"] for a in issue["events"] if a["event_key"])
        write_json(STATE / "event-history.json", list(dict.fromkeys(history))[-100:])
    return {"pdf": str(target), **result}


def publish(day, issue_path, demo=False):
    with (STATE / "print.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if not demo and read_json(day_dir(day)/"print.json"):
            raise ValueError("Этот выпуск уже передавался в очередь. Перезапись остановлена.")
        return _publish_unlocked(day,issue_path,demo=demo)


def fallback_unlocked(day):
    """No inference: explicitly labelled excerpts, never pretend to be summaries."""
    packet=read_json(day_dir(day)/"packet.json")
    if not packet or not (0 <= (now()-datetime.fromisoformat(packet["collected_at"])).total_seconds() <= 7200):
        packet=collect(day)
    if not packet.get("sources"):
        return None
    issue={"date":day,"packet_sha256":packet["packet_sha256"],"demo":False,
        "editorial_line":"РЕЗЕРВНЫЙ ВЫПУСК. AI-редактор недоступен: ниже расписание и исходные цитаты без саммари."}
    issue.update({key:[] for key in SECTIONS})
    kinds={"mail":"mail","telegram":"chats","news":"news"}
    mail_counts={"corporate":0,"personal":0}
    for source in packet["sources"]:
        section=kinds.get(source["kind"])
        if not section:
            continue
        account=source.get("account","corporate")
        if section=="mail" and mail_counts.get(account,0)>=2:
            continue
        if section!="mail" and len(issue[section])>=min(2,SECTIONS[section]):
            continue
        text=" ".join(source.get("text","").split())
        if not text:
            continue
        # Raw excerpts carry no semantic claim that a deadline or event is confirmed.
        issue[section].append({"headline":("Цитата: "+source.get("chat",source["title"]))[:65],
            "body":"«"+text[:280]+("..." if len(text)>280 else "")+"»",
            "source_ids":[source["id"]],"priority":2,"event_key":""})
        if section=="mail":
            mail_counts[account]=mail_counts.get(account,0)+1
    path=day_dir(day)/"fallback-issue.json"
    write_json(path,issue)
    return _publish_unlocked(day,path,fallback=True)


def import_calendar(path):
    data = read_json(path)
    if data.get("date") != now().date().isoformat():
        raise ValueError("Календарь должен относиться к сегодняшнему дню по Москве")
    data["checked_at"] = now().isoformat()
    events = []
    for item in data.get("events", []):
        if item.get("status") == "cancelled":
            continue
        e = calendar_event(item)
        value = e["start"]
        if not value:
            raise ValueError("Событие без даты")
        if len(value) > 10 and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Событие без часового пояса")
        events.append(e)
    data["events"] = events
    write_json(day_dir(data["date"]) / "calendar.json", data)
    return {"events": len(events), "date": data["date"]}


def print_tick(manual=False):
    cfg, moment = config(), now()
    if not cfg["print_enabled"] or not cfg.get("printer"):
        return {"state": "disabled"}
    minute = moment.strftime("%H:%M")
    hour, minute_number = map(int, cfg["print_time"].split(":"))
    window_start = moment.replace(hour=hour,minute=minute_number,second=0,microsecond=0)
    window_end = window_start + timedelta(minutes=5)
    day = moment.date().isoformat()
    d = day_dir(day)
    ledger_path = d / "print.json"
    with (STATE / "print.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = read_json(ledger_path)
        manual_pending = bool(ledger and ledger.get("manual"))
        expired_manual = manual_pending and moment >= datetime.fromisoformat(ledger["cancel_after"])
        if not manual and ((minute >= cfg["print_cutoff"] and not manual_pending) or expired_manual):
            if ledger and ledger.get("job_id") and not ledger.get("cutoff_checked"):
                if WINDOWS:
                    from windows_backend import cancel_pending
                    try:
                        if cancel_pending(cfg,ledger):
                            ledger['state']='cancelled_at_cutoff'
                        ledger['cutoff_checked']=True
                        write_json(ledger_path,ledger)
                        return {'state':'outside_window'}
                    except (RuntimeError,OSError,subprocess.TimeoutExpired):
                        return {'state':'queue_check_failed'}
                active = subprocess.run(["/usr/bin/lpstat", "-W", "not-completed", "-o", cfg["printer"]], capture_output=True, text=True, timeout=10)
                if active.returncode != 0:
                    return {"state": "queue_check_failed"}
                if any(line.split()[0] == ledger["job_id"] for line in active.stdout.splitlines() if line.strip()):
                    cancelled = subprocess.run(["/usr/bin/cancel", ledger["job_id"]], capture_output=True, timeout=10)
                    if cancelled.returncode != 0:
                        return {"state": "cancel_failed"}
                    ledger["state"] = "cancelled_at_cutoff"
                ledger["cutoff_checked"] = True
                write_json(ledger_path, ledger)
            return {"state": "outside_window"}
        if not manual and not (window_start <= moment <= window_end):
            return {"state": "outside_window"}
        if ledger:
            return {"state": ledger["state"]}
        ready = read_json(d / "ready.json")
        if manual and not ready:
            return {"state":"no_today_issue"}
        if not ready:
            try:
                fallback_unlocked(day)
                ready=read_json(d/"ready.json")
            except Exception as error:
                write_json(d/"fallback-error.json",{"error":type(error).__name__,"at":now().isoformat()})
                return {"state":"fallback_failed"}
            if now().date().isoformat()!=day or now()>window_end:
                return {"state":"outside_window"}
        if not ready or ready["date"] != day or ready.get("demo") is not False:
            return {"state": "no_today_issue"}
        packet = read_json(d / "packet.json")
        age = (moment - datetime.fromisoformat(packet["collected_at"])).total_seconds()
        if not (0 <= age <= 7200):
            return {"state": "stale_packet"}
        pdf = Path(ready["pdf"]).resolve()
        expected = (ISSUES / day / "газета.pdf").resolve()
        if pdf != expected or not pdf.is_file():
            raise ValueError("Неверный путь PDF")
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != ready["sha256"]:
            raise ValueError("PDF изменён после проверки")
        # Commit before submission: an interrupted/ambiguous request must never auto-repeat.
        ledger = {"date":day, "state":"submitting", "at":moment.isoformat(), "sha256":ready["sha256"]}
        if manual:
            ledger.update(manual=True,cancel_after=(moment+timedelta(minutes=30)).isoformat())
        write_json(ledger_path, ledger)
        try:
            if WINDOWS:
                from windows_backend import submit
                ledger.update(submit(cfg,pdf,day))
            else:
                result = subprocess.run(["/usr/bin/lp", "-d", cfg["printer"], "-n", "1", "-o", "media=A4",
                    "-o", "sides=one-sided", "-o", "job-sheets=none", "-t", cfg["title"] + " / " + day, str(pdf)],
                    capture_output=True, text=True, timeout=30, env={**os.environ, "LC_ALL":"C"})
                match = re.search(r"(?<![\w-])(" + re.escape(cfg["printer"]) + r"-\d+)(?!\d)", result.stdout)
                if result.returncode != 0 or not match:
                    ledger["state"] = "uncertain_or_failed"
                    ledger["detail"] = result.stderr.strip()[:500] or "Очередь не вернула распознаваемый номер задания"
                else:
                    ledger.update(state="submitted", job_id=match.group(1))
            if ledger['state']=='submitted':
                watermarks = read_json(STATE / "printed-telegram-ids.json", {})
                for chat_id, message_id in (packet.get("telegram_watermarks", {}) if ready.get("mode")!="fallback" else {}).items():
                    watermarks[chat_id] = max(message_id,watermarks.get(chat_id,0))
                write_json(STATE / "printed-telegram-ids.json",watermarks)
                if ready.get("meme_id"):
                    history = read_json(STATE / "printed-memes.json", [])
                    write_json(STATE / "printed-memes.json", (history + [ready["meme_id"]])[-30:])
        except (OSError, subprocess.TimeoutExpired):
            ledger["state"] = "uncertain_or_failed"
        write_json(ledger_path, ledger)
        return ledger


def schedule_files():
    folder = ROOT / "schedule"
    folder.mkdir(exist_ok=True)
    definitions = {
        AGENT_LABEL: {"Label":AGENT_LABEL, "ProgramArguments":[sys.executable, str(ROOT / "gazette.py"), "print-tick"],
            "StartInterval":60, "RunAtLoad":True, "WorkingDirectory":str(ROOT),
            "StandardOutPath":str(STATE / "print-agent.log"), "StandardErrorPath":str(STATE / "print-agent-error.log")},
        AWAKE_LABEL: {"Label":AWAKE_LABEL, "ProgramArguments":["/usr/bin/caffeinate", "-s"], "KeepAlive":True,"RunAtLoad":True}
    }
    for label, definition in definitions.items():
        (folder / (label + ".plist")).write_bytes(plistlib.dumps(definition))
    return list(definitions)


def install_schedule():
    if WINDOWS:
        from windows_backend import install_schedule as install_windows
        return install_windows()
    labels = schedule_files()
    target = Path.home() / "Library" / "LaunchAgents"
    target.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for label in labels:
        destination = target / (label + ".plist")
        if destination.exists():
            subprocess.run(["/bin/launchctl", "bootout", domain + "/" + label], capture_output=True)
        shutil.copyfile(ROOT / "schedule" / (label + ".plist"), destination)
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(destination)], check=True, capture_output=True)
    return {"installed":labels,"printing_enabled":config()["print_enabled"]}


def uninstall_schedule():
    if WINDOWS:
        from windows_backend import uninstall_schedule as uninstall_windows
        uninstall_windows()
        cfg=config()
        cfg['print_enabled']=False
        write_json(ROOT/'config.json',cfg)
        return {'installed':False}
    domain = f"gui/{os.getuid()}"
    for label in (AGENT_LABEL, AWAKE_LABEL):
        subprocess.run(["/bin/launchctl", "bootout", domain + "/" + label], capture_output=True)
        path = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
        if path.exists():
            path.unlink()
    cfg = config()
    cfg["print_enabled"] = False
    write_json(ROOT / "config.json", cfg)
    return {"installed":False}


def printers():
    if WINDOWS:
        from windows_backend import printers as windows_printers
        return windows_printers()
    r = subprocess.run(["/usr/bin/lpstat", "-p"], capture_output=True, text=True, env={**os.environ,"LC_ALL":"C"})
    return re.findall(r"^printer (\S+)", r.stdout, re.M)


def status():
    d = day_dir()
    return {"date":now().date().isoformat(),"current_time":now().isoformat(),"timezone":config()["timezone"],
        "yandex_connected":bool(secret("yandex")), "telegram_connected":bool(secret("telegram") and (STATE / "telegram.session").exists()),
        "calendar_imported_today":(d / "calendar.json").exists(), "calendar_ics_connected":bool(secret("calendar_ics")),
        "personal_mail_imported_today":(d / "personal-mail.json").exists(),
        "printer":config()["printer"], "print_enabled":config()["print_enabled"],
        "ready_today":(d / "ready.json").exists(), "print":read_json(d / "print.json"),
        "source_status":read_json(d / "packet.json", {}).get("status", {})}


def normalize_app_password(value):
    # App passwords are often copied in visual groups separated by spaces.
    return "".join(value.split())


def yandex_error_message(error, stage):
    raw = str(error).lower()
    codes = re.findall(r"\[(AUTHENTICATIONFAILED|AUTHORIZATIONFAILED|ALERT|UNAVAILABLE|PRIVACYREQUIRED)\]",str(error),re.I)
    code = " [" + codes[0].upper() + "]" if codes else ""
    if isinstance(error,(TimeoutError,ConnectionError,OSError)) and not isinstance(error,imaplib.IMAP4.error):
        return "Нет соединения с imap.yandex.ru:993. Проверь интернет и доступ к серверу. Пароль не сохранён."
    if stage == "mailbox":
        return "Вход принят, но сервер не дал открыть Входящие" + code + ". Проверь доступ IMAP к этому ящику."
    if "imap" in raw and any(word in raw for word in ("disabled","not enabled","запрещ")):
        return "Сервер сообщает, что IMAP отключён" + code + ". Включи его в настройках вышкинской почты и сохрани изменения."
    if "application" in raw and "password" in raw:
        return "Сервер требует пароль приложения типа «Почта»" + code + ". Создай его именно в подключаемом почтовом аккаунте."
    return ("Яндекс отклонил вход" + code + ". Пароль не сохранён.\n"
        "Проверь: полный адрес почты; пароль приложения типа «Почта» создан в ЭТОМ ЖЕ аккаунте;\n"
        "в настройках Почты включены IMAP и «Пароли приложений и OAuth-токены».\n"
        "Если всё совпадает, открой вышкинскую почту в браузере и проверь, нет ли запроса подтвердить вход.")


def setup_yandex():
    import ssl
    print("Подключение Яндекс Почты / реквизиты вводятся только на этом Mac.")
    print("Войди именно в подключаемый почтовый аккаунт. В настройках Почты включи IMAP и «Пароли приложений и OAuth-токены».")
    print("Пароль приложения должен быть создан для сервиса «Почта» в этом же аккаунте.")
    login = input("Полный адрес почты: ").strip().lower()
    password = normalize_app_password(getpass.getpass("Пароль приложения (ввод скрыт): "))
    if not login or not password:
        print("Пустой адрес или пароль. Ничего не сохранено.")
        return False
    if "@" not in login or not login.rsplit("@",1)[1]:
        print("Ожидается полный адрес почты, включая домен после @. Ничего не сохранено.")
        return False
    stage = "connection"
    try:
        with imaplib.IMAP4_SSL(config()["mail"]["host"], ssl_context=ssl.create_default_context(), timeout=25) as conn:
            stage = "login"
            conn.login(login,password)
            stage = "mailbox"
            result,_ = conn.select("INBOX",readonly=True)
            if result != "OK":
                raise imaplib.IMAP4.error("INBOX access denied")
        set_secret("yandex",{"login":login,"password":password})
        print("Почта подключена и проверена. Письма читаются без изменения статуса прочтения.")
        return True
    except (imaplib.IMAP4.error,OSError,TimeoutError) as error:
        print(yandex_error_message(error,stage))
        return False


async def login_telegram():
    from telethon import TelegramClient
    print("Открой https://my.telegram.org → API development tools. Это реквизиты приложения, без платного AI API.")
    api_id = int(input("API ID: ").strip())
    api_hash = getpass.getpass("API hash (ввод скрыт): ").strip()
    phone = input("Номер телефона с кодом страны: ").strip()
    client = TelegramClient(str(STATE / "telegram"), api_id, api_hash, receive_updates=False)
    try:
        await client.start(phone=phone, code_callback=lambda:getpass.getpass("Код из Telegram (ввод скрыт): "),
            password=lambda:getpass.getpass("Пароль двухэтапной защиты (если требуется): "))
        set_secret("telegram", {"api_id":api_id,"api_hash":api_hash})
        print("Telegram подключён. Сообщения и приглашения отправляться не будут.")
    finally:
        await client.disconnect()
        for path in STATE.glob("telegram.session*"):
            path.chmod(0o600)


def setup():
    while True:
        print("\n"+config()["title"]+" / Подключение источников\n"
              "1. Яндекс Почта\n2. Telegram\n3. Календарь по закрытой ICS-ссылке (необязательно при подключённом Google Calendar)\n"
              "4. Выбрать принтер и включить ежедневную печать\n5. Показать состояние\n"
              "6. Показать найденные чаты и уточнить одинаковые названия\n7. Отключить печать и защиту от сна\n0. Готово")
        choice = input("Выбери пункт: ").strip()
        try:
            if choice == "0":
                return
            if choice == "1":
                setup_yandex()
            elif choice == "2":
                asyncio.run(login_telegram())
            elif choice == "3":
                print("Google Calendar → настройки календаря → Интеграция → Закрытый адрес в формате iCal.")
                value = getpass.getpass("Закрытая HTTPS-ссылка (ввод скрыт, пусто — отмена): ").strip()
                if value:
                    host = urlparse(value).hostname or ""
                    if urlparse(value).scheme != "https" or host != "calendar.google.com":
                        raise ValueError("Ожидается закрытая ссылка calendar.google.com")
                    set_secret("calendar_ics", [value])
                    print("Календарь сохранён. Для нескольких календарей используй подключение Google Calendar в приложении.")
            elif choice == "4":
                available = printers()
                if not available:
                    print("Принтер пока не найден. Подключи его кабелем и добавь в настройках macOS.")
                    continue
                for i, name in enumerate(available,1):
                    print(f"{i}. {name}")
                index = int(input("Номер принтера: ")) - 1
                if index not in range(len(available)):
                    raise ValueError("Неверный номер")
                cfg = config()
                if WINDOWS:
                    from windows_backend import sumatra_path
                    sumatra_path(cfg)
                cfg.update(printer=available[index],print_enabled=False)
                write_json(ROOT / "config.json", cfg)
                install_schedule()
                cfg['print_enabled']=True
                write_json(ROOT/'config.json',cfg)
                print("Печать включена: ежедневно в 08:00 по Москве. Компьютер должен быть включён, на питании, приложение запущено.")
                if input("Напечатать один демонстрационный лист сейчас? [да/нет]: ").strip().lower() == "да":
                    demo_pdf = Path(demo()["pdf"])
                    if WINDOWS:
                        from windows_backend import submit
                        print(json.dumps(submit(cfg,demo_pdf,'demo-'+now().date().isoformat()),ensure_ascii=False))
                    else:
                        subprocess.run(["/usr/bin/lp","-d",cfg["printer"],"-n","1","-o","media=A4",str(demo_pdf)],check=True)
            elif choice == "5":
                print(json.dumps(status(),ensure_ascii=False,indent=2))
            elif choice == "6":
                packet = collect()
                inventory = read_json(STATE / "telegram-selected.json", [])
                print(json.dumps(inventory,ensure_ascii=False,indent=2))
                for w in packet["warnings"]:
                    print(w)
                print("Если имя не найдено или неоднозначно, укажи название и ID из списка диалогов.")
                if secret("telegram") and input("Показать все группы и каналы для выбора ID? [да/нет]: ").strip().lower() == "да":
                    async def show():
                        from telethon import TelegramClient
                        creds = secret("telegram")
                        async with TelegramClient(str(STATE / "telegram"),creds["api_id"],creds["api_hash"]) as client:
                            async for d in client.iter_dialogs():
                                if d.is_group or d.is_channel:
                                    print(f"{d.id}: {d.name}")
                    asyncio.run(show())
                    name = input("Название из config.json (пусто — выход): ").strip()
                    if name:
                        identifier = int(input("ID: ").strip())
                        cfg = config()
                        cfg["telegram"]["resolved_ids"][name] = identifier
                        write_json(ROOT / "config.json",cfg)
            elif choice == "7":
                uninstall_schedule()
                print("Печать и защита от сна отключены. Подготовку газеты можно отключить в расписаниях приложения.")
        except Exception as error:
            print(f"Не получилось: {type(error).__name__}. Проверь ввод и доступ к сервису; реквизиты не выводятся.")


def demo():
    day = now().date().isoformat()
    packet = {"date":day,"collected_at":now().isoformat(), "events":[
        {"id":"demo:calendar:1","kind":"calendar","title":"Семинар: теория вероятностей","start":day+"T10:00:00+03:00","end":day+"T11:20:00+03:00","location":"Покровский бульвар, аудитория 314","url":"https://calendar.google.com/"},
        {"id":"demo:calendar:2","kind":"calendar","title":"Встреча проектной команды","start":day+"T14:30:00+03:00","end":day+"T15:30:00+03:00","location":"Онлайн","url":"https://calendar.google.com/"},
        {"id":"demo:calendar:3","kind":"calendar","title":"Совместная пробежка","start":day+"T19:00:00+03:00","end":day+"T20:00:00+03:00","location":"Место сбора уточнить в чате","url":"https://calendar.google.com/"}],
        "sources":[],"meme":None,"warnings":[],"recent_event_keys":[],
        "status":{key:source_status("ok","Демонстрационные данные") for key in ("calendar","mail","personal_mail","telegram","news","tasks")}}
    packet["sources"] = packet["events"] + [
        {"id":"demo:mail","kind":"mail","account":"corporate","title":"Вымышленное письмо","url":"","text":"Демонстрация"},
        {"id":"demo:personal","kind":"mail","account":"personal","title":"Вымышленное письмо","url":"","text":"Демонстрация"},
        *[{"id":"demo:"+k,"kind":"news" if k=="news" else "telegram", "title":"Вымышленный пример", "url":"", "text":"Демонстрация"} for k in ("chat","news","event")]]
    seal(packet)
    def a(headline,body,sid,priority=3):
        return {"headline":headline,"body":body,"source_ids":[sid],"priority":priority,"event_key":""}
    issue={"date":day,"packet_sha256":packet["packet_sha256"],"demo":True,
        "editorial_line":"Три дела на день. Остальное пока можно не трогать.",
        "essentials":[a("Домашка - до 23:59","Загрузить решение в систему курса. Срок не переносили: сдать нужно сегодня.","demo:mail",5)],
        "mail":[a("Учебный офис просит документы","Отправить форму до пятницы, 18:00. Ссылка в письме. Закрой сразу, иначе будут доёбывать напоминаниями.","demo:mail"),
                a("Билет на выставку уже в почте","Вход в субботу с 14:00. Покажи билет на входе; повторно покупать его не нужно.","demo:personal")],
        "chats":[a("По проекту наконец договорились","Черновик - к четвергу, презентация - к пятнице. Ответственные в закрепе. Остальные 200 сообщений можно пропустить.","demo:chat")],
        "news":[a("Здесь будут два главных сюжета","Приоритет - Россия, затем важное в мире. Коротко: что произошло, что подтверждено и что пока неизвестно. Reuters, BBC и «Медуза».","demo:news")],
        "events":[a("Встреча с основателем компании","Пятница, 19:00, Москва. Открытый разговор и вопросы из зала. В настоящем выпуске здесь будут имя спикера, адрес и регистрация.","demo:event"),
                  a("Новая выставка фотографии","Суббота, с 14:00. В рабочем выпуске - площадка, даты и цена из выбранного источника.","demo:event")]}
    # Demo has its own packet and can never replace the production packet.
    from layout import render
    validate_issue(issue,packet)
    target=ROOT/"output"/"газета-пример.pdf"
    result=render(issue,packet,target)
    (ROOT/"output"/"газета-пример.md").write_text(full_text(issue,packet,result["omitted"]),encoding='utf-8')
    return {"pdf":str(target),**result}


def main():
    initialize()
    p=argparse.ArgumentParser(description="Личная утренняя газета")
    sub=p.add_subparsers(dest="command",required=True)
    for name in ("collect","status","demo","setup","personalize","keep-awake","setup-yandex","schedule-files","install-print-schedule","uninstall-print-schedule","print-tick","print-now","schema"):
        sub.add_parser(name)
    item=sub.add_parser("import-telegram-exports")
    item.add_argument("paths",nargs="+",type=Path)
    for name in ("import-calendar","import-personal-mail","ingest-news","publish"):
        item=sub.add_parser(name)
        item.add_argument("path",type=Path)
    args=p.parse_args()
    if args.command=="collect":
        packet=collect()
        result={"packet":str(day_dir()/"packet.json"),"packet_sha256":packet["packet_sha256"],"status":packet["status"],"warnings":packet["warnings"]}
    elif args.command=="import-calendar":
        result=import_calendar(args.path)
    elif args.command=="import-personal-mail":
        result=import_personal_mail(args.path)
    elif args.command=="import-telegram-exports":
        from telegram_export import import_exports
        data=import_exports(args.paths)
        result={"date":data["date"],"chats":[{"name":c["chat"],"messages":len(c["messages"])} for c in data["chats"]]}
    elif args.command=="ingest-news":
        packet=ingest_news(now().date().isoformat(),read_json(args.path))
        result={"packet_sha256":packet["packet_sha256"],"news_ids":[s["id"] for s in packet["sources"] if s["kind"]=="news"]}
    elif args.command=="publish":
        result=publish(now().date().isoformat(),args.path)
    elif args.command=="status":result=status()
    elif args.command=="demo":result=demo()
    elif args.command=="setup":setup();return
    elif args.command=="personalize":
        from onboarding import personalize
        result=personalize()
    elif args.command=='keep-awake':
        if not WINDOWS:
            raise ValueError('Эта команда предназначена для службы Windows')
        from windows_backend import keep_awake
        keep_awake()
        return
    elif args.command=="setup-yandex":
        setup_yandex()
        input("Нажми Enter, чтобы закрыть окно.")
        return
    elif args.command in ("print-tick","print-now"):
        result=print_tick(manual=args.command=="print-now")
        if result["state"] in ("disabled","outside_window"):
            return
    elif args.command=="schedule-files":result=schedule_files()
    elif args.command=="install-print-schedule":result=install_schedule()
    elif args.command=="uninstall-print-schedule":result=uninstall_schedule()
    elif args.command=="schema":
        write_json(ROOT/"issue-schema.json",issue_schema())
        result={"schema":str(ROOT/"issue-schema.json")}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    try:main()
    except (ValueError,FileNotFoundError) as e:
        print(str(e),file=sys.stderr)
        sys.exit(1)
