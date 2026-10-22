import asyncio
import email
import email.policy
import hashlib
import imaplib
import random
import re
import ssl
import urllib.request
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from common import STATE, config, day_dir, now, read_json, seal, secret, source_status, write_json


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "br", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_html(value):
    p = TextOnly()
    p.feed(value)
    return unescape(" ".join(p.parts)).strip()


def compact_text(value):
    value = re.sub(r"[\u034f\u200b-\u200f\u2060-\u206f\ufeff]", "", value)
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in value.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def mail_body(msg):
    if msg.is_multipart():
        part = msg.get_body(preferencelist=("plain", "html"))
    else:
        part = msg
    if not part:
        return ""
    try:
        text = part.get_content()
    except (LookupError, UnicodeError):
        text = part.get_payload(decode=True).decode("utf-8", "replace")
    if not isinstance(text, str):
        return ""
    return compact_text(clean_html(text) if part.get_content_type() == "text/html" else text)


def collect_mail(packet):
    creds = secret("yandex")
    if not creds:
        packet["status"]["mail"] = source_status("not_connected", "Почта не подключена")
        return
    cfg = config()["mail"]
    since = (now() - timedelta(days=cfg["lookback_days"])).strftime("%d-%b-%Y")
    count, skipped, shortened = 0, 0, 0
    with imaplib.IMAP4_SSL(cfg["host"], ssl_context=ssl.create_default_context(), timeout=30) as conn:
        conn.login(creds["login"], creds["password"])
        conn.select("INBOX", readonly=True)
        validity = conn.response("UIDVALIDITY")[1][0].decode()
        status, data = conn.uid("search", None, "SINCE", since)
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        uids = data[0].split()
        skipped += max(0, len(uids) - cfg["max_messages"])
        for uid in reversed(uids[-cfg["max_messages"]:]):
            status, sizes = conn.uid("fetch", uid, "(RFC822.SIZE)")
            size_text = str(sizes)
            match = re.search(r"RFC822.SIZE (\d+)", size_text)
            oversized = bool(match and int(match.group(1)) > 1_000_000)
            query = "(BODY.PEEK[HEADER])" if oversized else "(BODY.PEEK[] FLAGS)"
            status, fetched = conn.uid("fetch", uid, query)
            payloads = [x[1] for x in fetched if isinstance(x, tuple)] if status == "OK" else []
            if not payloads:
                skipped += 1
                continue
            msg = email.message_from_bytes(b"".join(payloads), policy=email.policy.default)
            sid = f"mail:{validity}:{uid.decode()}"
            text = mail_body(msg) if not oversized else "Тело письма больше 1 МБ: требуется открыть оригинал."
            attachments = [p.get_filename() for p in msg.walk() if p.get_filename()]
            packet["sources"].append({"id": sid, "kind": "mail", "account": "corporate", "title": str(msg.get("subject", "Без темы")),
                "from": str(msg.get("from", "")), "date": str(msg.get("date", "")),
                "url": "https://mail.yandex.ru/", "text": text[:2500],
                "attachments": attachments, "truncated": oversized or len(text) > 2500})
            shortened += int(oversized or len(text) > 2500)
            count += 1
    packet["status"]["mail"] = source_status("partial" if skipped or shortened else "ok",
        f"Проверено писем: {count}" + (f"; вне лимита или недоступно: {skipped}" if skipped else "") +
        (f"; укорочено: {shortened}" if shortened else ""))


def import_personal_mail(path):
    data = read_json(path)
    if not data or data.get("date") != now().date().isoformat():
        raise ValueError("Личная почта должна относиться к сегодняшнему дню по Москве")
    expected = config()["personal_mail"]["address"]
    if str(data.get("account","")).casefold() != expected.casefold():
        raise ValueError("Подключён другой аккаунт личной почты")
    if len(data.get("messages",[])) > config()["personal_mail"]["max_messages"]:
        raise ValueError("Превышен лимит писем личной почты")
    normalized = []
    for item in data.get("messages",[]):
        if not item.get("id"):
            raise ValueError("У письма отсутствует ID")
        text = str(item.get("text", ""))
        text = compact_text(clean_html(text) if item.get("is_html") else text)
        normalized.append({"id":"gmail:"+str(item["id"]),"kind":"mail","account":"personal",
            "title":str(item.get("title") or item.get("subject") or "Без темы"),
            "from":str(item.get("from") or ""),"date":str(item.get("date") or ""),
            "url":str(item.get("url") or "https://mail.google.com/"),
            "text":text[:2500],"truncated":len(text)>2500})
    data.update(messages=normalized,checked_at=now().isoformat())
    write_json(day_dir(data["date"])/"personal-mail.json",data)
    return {"messages":len(normalized),"date":data["date"]}


def collect_personal_mail(packet):
    data=read_json(day_dir(packet["date"])/"personal-mail.json")
    if not data:
        packet["status"]["personal_mail"]=source_status("not_checked","Личная почта ещё не проверена")
        return
    if not (0 <= (now()-datetime.fromisoformat(data["checked_at"])).total_seconds() <= 7200):
        packet["status"]["personal_mail"]=source_status("error","Выгрузка личной почты устарела")
        return
    packet["sources"].extend(data["messages"])
    errors=data.get("errors",[])
    packet["warnings"].extend(errors)
    detail=f"Прочитано писем: {len(data['messages'])}"
    if data.get("reviewed_count") is not None:
        detail += f"; просмотрено кандидатов: {data['reviewed_count']}"
    packet["status"]["personal_mail"]=source_status("partial" if errors else "ok",detail)


def calendar_event(raw, prefix="calendar"):
    start = raw.get("start")
    end = raw.get("end")
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("date")
    if isinstance(end, dict):
        end = end.get("dateTime") or end.get("date")
    uncertain_time = bool(raw.get("time_unspecified"))
    original_start = raw.get("original_start")
    # Some connector responses represent all-day events as naive midnight.
    # Do not turn that ambiguity into an invented timed appointment.
    if isinstance(start, str) and len(start) > 10 and datetime.fromisoformat(start.replace("Z", "+00:00")).tzinfo is None:
        uncertain_time, original_start = True, start
        start = start[:10]
        end = end[:10] if isinstance(end, str) else end
    sid = str(raw.get("id") or hashlib.sha256(str(raw).encode()).hexdigest()[:16])
    return {"id": f"{prefix}:{sid}" if not sid.startswith(prefix + ":") else sid,
        "kind": "calendar", "title": str(raw.get("title") or raw.get("summary") or "Без названия"),
        "start": start, "end": end, "time_unspecified": uncertain_time, "original_start": original_start,
        "location": str(raw.get("location") or ""),
        "url": raw.get("url") or raw.get("htmlLink") or "https://calendar.google.com/"}


def collect_calendar(packet):
    imported = read_json(day_dir(packet["date"]) / "calendar.json")
    if imported and imported.get("date") == packet["date"]:
        checked = datetime.fromisoformat(imported["checked_at"])
        if abs((now() - checked).total_seconds()) < 7200:
            events = [calendar_event(e) for e in imported.get("events", []) if e.get("status") != "cancelled"]
            packet["events"] = events
            packet["sources"].extend(events)
            errors = imported.get("calendar_errors", [])
            packet["warnings"].extend(errors)
            packet["status"]["calendar"] = source_status("partial" if errors else "ok", f"Событий: {len(events)}" + ("; часть календарей недоступна" if errors else ""))
            packet["status"]["tasks"] = source_status("ok" if imported.get("tasks_checked") else "not_connected",
                "Задачи проверены" if imported.get("tasks_checked") else "Отдельный список задач не подключён")
            for item in imported.get("tasks", []):
                packet["sources"].append({"id": "task:" + str(item["id"]), "kind": "task", **{k:v for k,v in item.items() if k != "id"}})
            return
    urls = secret("calendar_ics") or []
    if not urls:
        packet["status"]["calendar"] = source_status("not_connected", "Ожидается выгрузка Google Calendar")
        packet["status"]["tasks"] = source_status("not_connected", "Отдельный список задач не подключён")
        return
    import icalendar
    import recurring_ical_events
    tz = ZoneInfo(config()["timezone"])
    begin = datetime.combine(date.fromisoformat(packet["date"]), datetime.min.time(), tzinfo=tz)
    finish = begin + timedelta(days=1)
    events = []
    for n, url in enumerate(urls):
        if urlparse(url).scheme != "https":
            raise ValueError("ICS must use HTTPS")
        with urllib.request.urlopen(url, timeout=25, context=ssl.create_default_context()) as response:
            data = response.read(10_000_001)
        if len(data) > 10_000_000:
            raise ValueError("ICS too large")
        cal = icalendar.Calendar.from_ical(data)
        for event in recurring_ical_events.of(cal).between(begin, finish):
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            start = event.decoded("DTSTART")
            end = event.decoded("DTEND", start)
            if isinstance(start, datetime) and start.tzinfo is None:
                start = start.replace(tzinfo=tz)
            if isinstance(end, datetime) and end.tzinfo is None:
                end = end.replace(tzinfo=tz)
            sid = f"calendar:{n}:{event.get('UID')}:{start.isoformat()}"
            events.append({"id": sid, "kind": "calendar", "title": str(event.get("SUMMARY", "Без названия")),
                "start": start.isoformat(), "end": end.isoformat(), "location": str(event.get("LOCATION", "")),
                "url": str(event.get("URL", "https://calendar.google.com/"))})
    packet["events"] = events
    packet["sources"].extend(events)
    packet["status"]["calendar"] = source_status("ok", f"Событий: {len(events)} (ICS)")
    packet["status"]["tasks"] = source_status("not_connected", "Отдельные Google Tasks не входят в ICS")


def folder_matches(folder, dialog):
    from telethon import utils
    did = dialog.id
    ids = lambda name: {utils.get_peer_id(p) for p in (getattr(folder, name, []) or [])}
    if did in ids("exclude_peers"):
        return False
    if did in ids("include_peers") | ids("pinned_peers"):
        return True
    if getattr(folder, "exclude_read", False) and not dialog.unread_count:
        return False
    if getattr(folder, "exclude_archived", False) and dialog.archived:
        return False
    mute_until = getattr(getattr(dialog.dialog, "notify_settings", None), "mute_until", None)
    if getattr(folder, "exclude_muted", False) and mute_until and mute_until > now():
        return False
    entity = dialog.entity
    if dialog.is_group:
        return bool(getattr(folder, "groups", False))
    if dialog.is_channel:
        return bool(getattr(folder, "broadcasts", False))
    if getattr(entity, "bot", False):
        return bool(getattr(folder, "bots", False))
    return bool(getattr(folder, "contacts" if getattr(entity, "contact", False) else "non_contacts", False))


async def collect_telegram(packet):
    creds = secret("telegram")
    if not creds:
        packet["status"]["telegram"] = source_status("not_connected", "Telegram не подключён")
        return
    from telethon import TelegramClient, functions, utils
    cfg = config()["telegram"]
    client = TelegramClient(str(STATE / "telegram"), creds["api_id"], creds["api_hash"],
        receive_updates=False, request_retries=1, connection_retries=1, flood_sleep_threshold=0)
    warnings, count = [], 0
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session expired")
        dialogs = await client.get_dialogs()
        folder_result = await client(functions.messages.GetDialogFiltersRequest())
        folders = getattr(folder_result, "filters", folder_result)
        selected = {}
        for name in cfg["folders"]:
            matches = [f for f in folders if str(getattr(getattr(f, "title", ""), "text", getattr(f, "title", ""))).casefold() == name.casefold()]
            if not matches:
                warnings.append(f"Не найдена папка {name}")
            for folder in matches:
                for d in dialogs:
                    if d.is_group and folder_matches(folder, d):
                        selected[d.id] = (d, "digest")
        names = [(name, mode) for mode in ("digest", "business", "running", "exhibitions") for name in cfg[mode]]
        if cfg.get("meme"):
            names.append((cfg["meme"], "meme"))
        for name, mode in names:
            resolved = cfg.get("resolved_ids", {}).get(name)
            matches = [d for d in dialogs if d.id == resolved] if resolved else [d for d in dialogs if d.name.casefold() == name.casefold()]
            if len(matches) != 1:
                warnings.append(f"{name}: " + ("не найден" if not matches else "несколько совпадений, выбери ID в настройках"))
                continue
            selected[matches[0].id] = (matches[0], mode)
        inventory = [{"id": d.id, "name": d.name, "mode": mode} for d, mode in selected.values()]
        write_json(STATE / "telegram-selected.json", inventory)
        chars_left = cfg.get("max_text_characters_total",90000)
        chat_budget = min(cfg.get("max_text_characters_per_chat",6000),
            max(500,chars_left // max(1,sum(mode != "meme" for _,mode in selected.values()))))
        cutoff = now() - timedelta(days=cfg["max_lookback_days"])
        previous = read_json(STATE / "last-published.json", {}).get("collected_at")
        printed_watermarks = read_json(STATE / "printed-telegram-ids.json", {})
        announcements_since = datetime.fromisoformat(previous) if previous else now() - timedelta(days=3)
        announcements_since = max(announcements_since, cutoff)
        candidates = []
        for d, mode in selected.values():
            if count >= cfg["max_messages_total"] and mode != "meme":
                warnings.append(f"{d.name}: общий лимит сообщений")
                continue
            if mode == "digest" and not d.unread_count:
                continue
            floor_id = max(getattr(d.dialog, "read_inbox_max_id", 0), printed_watermarks.get(str(d.id),0)) if mode == "digest" else 0
            earliest = cutoff if mode in ("digest", "meme") else announcements_since
            found, capped, chat_chars, shortened = 0, False, 0, 0
            limit = 80 if mode == "meme" else cfg["max_messages_per_chat"] + 1
            try:
                async for message in client.iter_messages(d.entity, limit=limit, min_id=floor_id):
                    if message.date < earliest:
                        break
                    if mode != "meme" and (found >= cfg["max_messages_per_chat"] or count >= cfg["max_messages_total"]):
                        capped = True
                        break
                    username = getattr(d.entity, "username", None)
                    internal = str(abs(d.id))
                    if internal.startswith("100"):
                        internal = internal[3:]
                    url = f"https://t.me/{username}/{message.id}" if username else (f"https://t.me/c/{internal}/{message.id}" if d.is_channel else "")
                    sid = f"tg:{d.id}:{message.id}"
                    if mode == "meme":
                        if message.photo:
                            candidates.append((message, sid, url, d.name))
                        continue
                    text = message.raw_text or ""
                    file = getattr(message, "file", None)
                    if not text and not file:
                        continue
                    allowance = min(1200,chat_budget-chat_chars,chars_left)
                    if allowance <= 0:
                        capped = True
                        break
                    clipped = text[:allowance]
                    chat_chars += len(clipped)
                    chars_left -= len(clipped)
                    shortened += int(len(clipped) < len(text))
                    packet["sources"].append({"id": sid, "kind": "telegram", "chat": d.name, "mode": mode,
                        "title": d.name, "date": message.date.isoformat(), "url": url,
                        "text": clipped, "attachment": getattr(file, "name", None),
                        "reply_to": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
                        "truncated": len(clipped) < len(text)})
                    found += 1
                    count += 1
                    packet.setdefault("telegram_watermarks", {})[str(d.id)] = max(message.id, packet.get("telegram_watermarks", {}).get(str(d.id),0))
                if capped:
                    warnings.append(f"{d.name}: сводка ограничена {found} сообщениями")
                    packet.get("telegram_watermarks", {}).pop(str(d.id),None)
                if shortened:
                    warnings.append(f"{d.name}: укорочено длинных сообщений: {shortened}")
            except Exception as error:
                warnings.append(f"{d.name}: {type(error).__name__}")
        recent = read_json(STATE / "printed-memes.json", [])[-30:]
        fresh = [x for x in candidates if x[1] not in recent]
        if fresh:
            choice = random.Random(packet["date"]).choice(fresh)
            message, sid, url, name = choice
            destination = day_dir(packet["date"]) / "meme.jpg"
            saved = await client.download_media(message.photo, file=str(destination))
            packet["meme"] = {"id": sid, "path": str(saved), "url": url, "caption": (message.raw_text or "")[:180], "chat": name}
        elif candidates:
            warnings.append("Все найденные мемы уже печатались в последних 30 выпусках")
        else:
            warnings.append("Свежий мем-картинка не найден")
        packet["status"]["telegram"] = source_status("partial" if warnings else "ok", f"Чатов выбрано: {len(selected)}; сообщений: {count}")
        packet["warnings"].extend(warnings)
    finally:
        await client.disconnect()


def collect(day=None):
    day = day or now().date().isoformat()
    packet = {"date": day, "collected_at": now().isoformat(), "events": [], "sources": [], "meme": None,
        "status": {}, "warnings": [], "recent_event_keys": read_json(STATE / "event-history.json", [])[-100:]}
    for key, function in (("calendar", collect_calendar), ("mail", collect_mail), ("personal_mail", collect_personal_mail)):
        try:
            function(packet)
        except Exception as error:
            packet["status"][key] = source_status("error", f"Ошибка подключения ({type(error).__name__})")
    try:
        from telegram_export import collect_exports
        if not collect_exports(packet):
            asyncio.run(asyncio.wait_for(collect_telegram(packet), timeout=240))
    except Exception as error:
        packet["status"]["telegram"] = source_status("error", f"Ошибка подключения ({type(error).__name__})")
    packet["status"]["news"] = source_status("not_checked", "Новости ещё не проверены")
    seal(packet)
    write_json(day_dir(day) / "packet.json", packet)
    return packet


def ingest_news(day, news):
    packet = read_json(day_dir(day) / "packet.json")
    if not packet:
        raise ValueError("Сначала соберите данные")
    allowed = config()["news"]["allowed_domains"]
    normalized = []
    for item in news["items"]:
        host = (urlparse(item["url"]).hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in allowed):
            raise ValueError(f"Источник вне разрешённого списка: {host}")
        if urlparse(item["url"]).scheme != "https":
            raise ValueError("Источник новостей должен использовать HTTPS")
        normalized.append({"id": "news:" + hashlib.sha256(item["url"].encode()).hexdigest()[:12],
            "kind": "news", "title": item["title"], "url": item["url"],
            "published_at": item["published_at"], "text": item["text"][:3500], "checked_at": now().isoformat()})
    packet["sources"] = [s for s in packet["sources"] if s["kind"] != "news"] + normalized
    packet["status"]["news"] = source_status("ok" if normalized else "error",
        f"Проверено публикаций: {len(normalized)}" if normalized else "Не удалось получить проверенные новости")
    seal(packet)
    write_json(day_dir(day) / "packet.json", packet)
    return packet
