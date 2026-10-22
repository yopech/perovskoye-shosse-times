"""Measured, monochrome, one-page A4 newspaper. Text never shrinks to fit."""
import io
import re
import os
from email.utils import parseaddr
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph
from pypdf import PdfReader

from common import ROOT, config, mail_groups, section_name


def fonts():
    windows=os.name=='nt'
    base = Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts' if windows else Path("/System/Library/Fonts/Supplemental")
    filenames=(("Gazette","georgia.ttf"),("GazetteBold","georgiab.ttf"),("GazetteItalic","georgiai.ttf"),
        ("GazetteSans","arial.ttf"),("GazetteSansBold","arialbd.ttf")) if windows else (("Gazette", "Georgia.ttf"), ("GazetteBold", "Georgia Bold.ttf"),
                           ("GazetteItalic", "Georgia Italic.ttf"), ("GazetteSans", "Arial.ttf"),
                           ("GazetteSansBold", "Arial Bold.ttf"))
    for name, filename in filenames:
        if name not in pdfmetrics.getRegisteredFontNames():
            if not (base/filename).is_file():
                raise FileNotFoundError('Не найден системный шрифт '+str(base/filename)+'. Нужны Georgia и Arial.')
            pdfmetrics.registerFont(TTFont(name, str(base / filename)))
    if "GazetteMasthead" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("GazetteMasthead",str(ROOT/"assets/fonts/PerovskoyeMasthead.ttf")))


def printable(text, font="Gazette"):
    text = str(text).replace("\u00a0", " ").replace("—", "-").replace("–", "-").replace("…", "...")
    cmap = pdfmetrics.getFont(font).face.charToGlyph
    return "".join(c for c in text if ord(c) in cmap or c in "\n\t")


def paragraph(text, width, font="Gazette", size=10.5, leading=None, markup=False):
    style = ParagraphStyle("p", fontName=font, fontSize=size, leading=leading or size * 1.3,
        alignment=TA_LEFT, textColor=colors.black, splitLongWords=True, spaceAfter=0)
    content = printable(text, font)
    if not markup:
        content = escape(content).replace("\n", "<br/>")
    p = Paragraph(content, style)
    _, height = p.wrap(width, 10000)
    return p, height


def draw_p(canvas, text, x, top, width, **kwargs):
    p, h = paragraph(text, width, **kwargs)
    p.drawOn(canvas, x, top - h)
    return top - h


def source_line(article, sources):
    chunks = []
    seen = set()
    for sid in article.get("source_ids", []):
        s = sources[sid]
        title = s.get("chat") or s.get("from") or urlparse(s.get("url", "")).hostname or s.get("title", "Источник")
        if s.get("kind") == "mail":
            sender, address = parseaddr(s.get("from", ""))
            if "figma" in address:
                title = "Figma"
            else:
                title = "Письмо" if s.get("account", "corporate") == "corporate" else "Gmail"
        title = printable(title, "GazetteSans")[:60]
        if title in seen:
            continue
        seen.add(title)
        url = s.get("url", "")
        if urlparse(url).scheme in ("http", "https"):
            chunks.append(f'<a href="{escape(url, quote=True)}" color="black">{escape(title)}</a>')
        else:
            chunks.append(escape(title))
    return " · ".join(chunks)


def article_height(article, width, sources):
    return (paragraph(article["headline"], width, "GazetteBold", 12, 14.3)[1] +
            paragraph(article["body"], width)[1] +
            paragraph(source_line(article, sources), width, "GazetteSans", 7.2, 9, markup=True)[1] + 14)


def draw_article(canvas, article, x, top, width, sources):
    top = draw_p(canvas, article["headline"], x, top, width, font="GazetteBold", size=12, leading=14.3) - 3
    top = draw_p(canvas, article["body"], x, top, width) - 4
    top = draw_p(canvas, source_line(article, sources), x, top, width,
        font="GazetteSans", size=7.2, leading=9, markup=True) - 7
    return top


def section_title(canvas, title, x, top, width):
    canvas.setLineWidth(0.5)
    canvas.line(x, top - 1, x + width, top - 1)
    top -= 7
    return draw_p(canvas, title.upper(), x, top, width, font="GazetteSansBold", size=8.1, leading=10) - 10


def time_label(event):
    if event.get("time_unspecified"):
        return "УТОЧН."
    start = event["start"]
    if len(start) == 10:
        return "ДЕНЬ"
    value = datetime.fromisoformat(start.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Событие без часового пояса")
    return value.astimezone(ZoneInfo(config()["timezone"])).strftime("%H:%M")


def agenda_height(events, width):
    return sum(paragraph(e["title"] + ("\n" + e["location"] if e.get("location") else ""), width - 43,
        "Gazette", 10.5, 13.3)[1] + 9 for e in events)


def draw_agenda(canvas, packet, x, top, width):
    top = section_title(canvas, "01 / " + section_name("agenda"), x, top, width)
    calendar_state = packet["status"].get("calendar", {}).get("state")
    if calendar_state not in ("ok", "partial"):
        return draw_p(canvas, "Календарь не удалось проверить. Расписание требует проверки в оригинале.", x, top, width) - 12
    if calendar_state == "partial":
        top = draw_p(canvas, "Часть календарей недоступна; ниже только полученные события.", x, top, width, font="GazetteItalic") - 10
    if not packet["events"]:
        return draw_p(canvas, "В календарях на сегодня событий нет.", x, top, width) - 12
    for event in sorted(packet["events"], key=lambda e: (len(e["start"]) != 10, e["start"])):
        draw_p(canvas, time_label(event), x, top - 1, 39, font="GazetteSansBold", size=9, leading=12)
        location = event.get("location", "")
        if urlparse(location).scheme in ("https", "http"):
            location = "Онлайн / ссылка в календаре"
        text = event["title"] + ("\n" + location if location else "")
        if event.get("time_unspecified"):
            text += "\nВремя не указано в выгрузке"
        top = draw_p(canvas, text, x + 43, top, width - 43, size=10.5, leading=13.3) - 9
    return top - 3


def render(issue, packet, target):
    fonts()
    target = Path(target)
    width, height = A4
    margin, gap = 34, 23
    colw = (width - 2 * margin - gap) / 2
    xright = margin + colw + gap
    sources = {s["id"]: s for s in packet["sources"]}
    buffer = io.BytesIO()
    c = Canvas(buffer, pagesize=A4, pageCompression=1)
    c.setTitle(config()["title"] + " / " + issue["date"])
    c.setAuthor("Личная утренняя редакция")
    date = datetime.fromisoformat(issue["date"])
    weekdays = ["ПОНЕДЕЛЬНИК", "ВТОРНИК", "СРЕДА", "ЧЕТВЕРГ", "ПЯТНИЦА", "СУББОТА", "ВОСКРЕСЕНЬЕ"]
    c.setStrokeColor(colors.black)
    c.setLineWidth(2)
    c.line(margin, height - 32, width - margin, height - 32)
    draw_p(c, "ЛИЧНАЯ ГАЗЕТА / МОСКВА", margin, height - 40,
        width - 2 * margin, font="GazetteSans", size=7.2, leading=9)
    title = config()["title"]
    title_size = min(44, (width - 2 * margin) * 44 / pdfmetrics.stringWidth(title, "GazetteMasthead", 44))
    c.setFont("GazetteMasthead", title_size)
    c.drawCentredString(width / 2, height - 91, title)
    draw_p(c, config()["tagline"], margin, height - 102, width - 2 * margin,
        font="GazetteItalic", size=9.2, leading=11)
    c.setLineWidth(0.5)
    c.line(margin, height - 121, width - margin, height - 121)
    label = f"{weekdays[date.weekday()]}  /  {date:%d.%m.%Y}"
    c.setFont("GazetteSansBold", 8)
    c.drawString(margin, height - 134, label)
    c.drawRightString(width - margin, height - 134, "ДЕМОНСТРАЦИЯ / ВСЁ НИЖЕ ВЫМЫШЛЕНО" if issue["demo"] else "ОДИН ЛИСТ / ПЯТЬ МИНУТ")
    top = height - 150
    if issue["editorial_line"].strip():
        top = draw_p(c, issue["editorial_line"], margin, top, width - 2 * margin,
            font="GazetteItalic", size=11, leading=14) - 11
    if issue["essentials"]:
        start = top
        top = draw_p(c, "НЕ ПРОЕБАТЬ", margin + 9, top - 9, width - 2 * margin - 18,
            font="GazetteSansBold", size=8, leading=10) - 6
        for i, item in enumerate(issue["essentials"], 1):
            top = draw_p(c, f"{i}. {item['headline']}. {item['body']}", margin + 9, top,
                width - 2 * margin - 18, size=10.5, leading=13.2) - 5
        c.setLineWidth(0.7)
        c.rect(margin, top - 2, width - 2 * margin, start - top + 2, stroke=1, fill=0)
        top -= 16
    body_top, bottom = top, 83
    omitted = []
    left = draw_agenda(c, packet, margin, top, colw)
    if left < bottom:
        raise ValueError("Обязательное расписание не помещается на страницу: нужен второй лист")
    for key, label in (("mail", "02 / " + section_name("mail")), ("chats", "03 / " + section_name("chats"))):
        articles = issue[key]
        if not articles:
            continue
        if key == "mail":
            section_drawn = False
            for account, group_label, group in mail_groups(articles,sources):
                ordered = sorted(group,key=lambda a:a["priority"],reverse=True)
                group_height = paragraph(group_label,colw,"GazetteSansBold",8.5,11)[1]+6
                heading_height = 0 if section_drawn else 30
                if not any(left-heading_height-group_height-article_height(a,colw,sources)>=bottom for a in ordered):
                    omitted.extend(a["headline"] for a in ordered)
                    continue
                if not section_drawn:
                    left=section_title(c,label,margin,left,colw)
                    section_drawn=True
                left=draw_p(c,group_label,margin,left,colw,font="GazetteSansBold",size=8.5,leading=11)-6
                for article in ordered:
                    h=article_height(article,colw,sources)
                    if left-h<bottom:
                        omitted.append(article["headline"])
                        continue
                    left=draw_article(c,article,margin,left,colw,sources)
            continue
        # Reserve a visible note for every omitted section instead of clipping content.
        if left - 30 < bottom:
            omitted.extend(a["headline"] for a in articles)
            continue
        left = section_title(c, label, margin, left, colw)
        for article in sorted(articles, key=lambda a: a["priority"], reverse=True):
            h = article_height(article, colw, sources)
            if left - h < bottom:
                omitted.append(article["headline"])
                continue
            left = draw_article(c, article, margin, left, colw, sources)
    right = body_top
    meme = packet.get("meme")
    reserve = 164 if meme and Path(meme["path"]).is_file() else 76
    for key, label in (("news", "04 / " + section_name("news")), ("events", "05 / " + section_name("events"))):
        articles = issue[key]
        if not articles:
            continue
        if right - 30 < bottom + reserve:
            omitted.extend(a["headline"] for a in articles)
            continue
        right = section_title(c, label, xright, right, colw)
        for article in sorted(articles, key=lambda a: a["priority"], reverse=True):
            h = article_height(article, colw, sources)
            if right - h < bottom + reserve:
                omitted.append(article["headline"])
                continue
            right = draw_article(c, article, xright, right, colw, sources)
    right = section_title(c, "06 / " + section_name("meme"), xright, right, colw)
    if meme and Path(meme["path"]).is_file():
        image = ImageReader(meme["path"])
        iw, ih = image.getSize()
        maxh = min(134, right - bottom - 32)
        if maxh < 20:
            raise ValueError("Нет места для мема")
        scale = min(colw / iw, maxh / ih)
        c.drawImage(image, xright + (colw - iw * scale) / 2, right - ih * scale,
            width=iw * scale, height=ih * scale, preserveAspectRatio=True, mask="auto")
        right -= ih * scale + 5
        draw_p(c, meme.get("caption") or meme["chat"], xright, right, colw,
            font="GazetteSans", size=7.5, leading=9)
    else:
        text = "В чате 400 сообщений.\nОтвет на вопрос: «Хуй знает»." if issue["demo"] else "Картинку не получили."
        draw_p(c, text, xright, right, colw, font="GazetteItalic", size=10.5, leading=13.3)
    c.setLineWidth(0.35)
    c.line(width / 2, bottom, width / 2, body_top)
    c.setLineWidth(0.7)
    c.line(margin, 72, width - margin, 72)
    state_labels = {"calendar": "Календарь", "mail": "Корпочта", "personal_mail":"Личная", "telegram": "Telegram", "news": "Новости", "tasks": "Задачи"}
    status_names = {"ok": "проверено", "partial": "частично", "error": "ошибка", "not_connected": "не подключено", "not_checked": "не проверено"}
    statuses = " / ".join(f"{label}: {status_names.get(packet['status'].get(key, {}).get('state'), 'не проверено')}" for key, label in state_labels.items())
    draw_p(c, statuses, margin, 65, width - 2 * margin, font="GazetteSans", size=7, leading=9)
    foot = f"Срез: {packet['collected_at'][11:16]} МСК. Ссылки кликабельны в PDF."
    if any(s.get("export_snapshot") for s in packet["sources"]):
        foot += " Чаты: ручные выгрузки, прочтение неизвестно."
    if omitted:
        foot += f" На бумагу не вошло заметок: {len(omitted)}; они сохранены в полном выпуске."
    draw_p(c, foot, margin, 43, width - 2 * margin, font="GazetteSans", size=7, leading=9)
    c.showPage()
    c.save()
    data = buffer.getvalue()
    pdf = PdfReader(io.BytesIO(data))
    if len(pdf.pages) != 1:
        raise ValueError("PDF must have exactly one page")
    if not pdf.pages[0].extract_text().strip():
        raise ValueError("Пустой PDF")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"pages": 1, "omitted": omitted, "bytes": len(data)}
