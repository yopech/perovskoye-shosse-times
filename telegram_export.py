"""Read Telegram Desktop HTML as data. Never execute embedded scripts."""
import hashlib
import re
import random
import shutil
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from common import STATE, config, day_dir, now, read_json, seal, source_status, write_json


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def has(self, cls):
        return cls in self.attrs.get('class', '').split()

    def find(self, cls):
        return next((n for n in self.walk() if n.has(cls)), None)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return ''.join(c.text() if isinstance(c, Node) else c for c in self.children)


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.stack[-1].children.append(n)
        if tag == 'br':
            n.children.append('\n')
        if tag not in ('br', 'img', 'meta', 'link', 'hr', 'input', 'source', 'wbr'):
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not any(n.tag in ('script', 'style') for n in self.stack):
            self.stack[-1].children.append(data)


def parse_export(folder):
    folder = Path(folder).resolve()
    files = sorted(folder.glob('messages*.html'))
    if not files:
        raise ValueError('Нет messages.html в выгрузке')
    messages, name, author = {}, '', ''
    for file in files:
        root = Document(file.read_text(encoding='utf-8')).root
        header = root.find('page_header')
        if header and not name:
            name = header.text().strip()
        for node in root.walk():
            if not node.has('message') or not node.has('default'):
                continue
            number = node.attrs.get('id', '')
            date = node.find('date')
            if not date:
                continue
            stamp = datetime.strptime(date.attrs['title'], '%d.%m.%Y %H:%M:%S UTC%z')
            sender = node.find('from_name')
            if sender:
                author = sender.text().strip()
            body = node.find('text')
            text = body.text().strip() if body else ''
            links = []
            if body:
                links = [n.attrs['href'] for n in body.walk() if n.tag == 'a' and n.attrs.get('href', '').startswith('https://')]
            photos = []
            for n in node.walk():
                if n.has('photo_wrap') and n.attrs.get('href'):
                    candidate = (folder / n.attrs['href']).resolve()
                    if candidate.is_relative_to(folder) and candidate.is_file():
                        photos.append(str(candidate))
            messages[number] = {'message_id':number, 'date':stamp.isoformat(), 'author':author,
                'text':text, 'links':links, 'photos':photos, 'export_page':str(file)}
    return {'chat':name, 'folder':str(folder), 'messages':list(messages.values())}


def import_exports(folders):
    day = now().date().isoformat()
    data = {'date':day, 'imported_at':now().isoformat(), 'chats':[parse_export(f) for f in folders]}
    for chat in data['chats']:
        key = hashlib.sha256(chat['chat'].encode()).hexdigest()[:12]
        for message in chat['messages']:
            message['id'] = f'tg-export:{key}:{message["message_id"]}'
    write_json(day_dir(day)/'telegram-exports.json', data)
    return data


def collect_exports(packet):
    data = read_json(day_dir(packet['date'])/'telegram-exports.json')
    if not data or data.get('date') != packet['date']:
        return False
    count = 0
    meme_name=config()['telegram'].get('meme','').casefold()
    meme_candidates=[]
    for chat in data['chats']:
        meme = bool(meme_name and chat['chat'].casefold()==meme_name)
        for m in chat['messages']:
            if meme:
                if m['photos']:
                    meme_candidates.append((chat,m))
                continue
            mode=next((k for k in ('business','running','exhibitions') if chat['chat'] in config()['telegram'].get(k,[])),'digest')
            packet['sources'].append({'id':m['id'], 'kind':'telegram', 'chat':chat['chat'],
                'mode':mode,
                'title':chat['chat'], 'date':m['date'], 'author':m['author'],
                'text':m['text'], 'url':'', 'links':m['links'], 'export_snapshot':True})
            count += 1
    recent=read_json(STATE/'printed-memes.json',[])[-30:]
    candidates=[(c,m) for c,m in meme_candidates if m['id'] not in recent]
    if candidates:
        chat,m=random.Random(packet['date']).choice(candidates)
        packet['meme']=meme_record(chat,m,0,packet['date'])
    packet['status']['telegram'] = source_status('partial', f'Ручная выгрузка: {len(data["chats"])} чатов, {count} сообщений; статус прочтения неизвестен')
    packet['warnings'].append('Telegram: только переданные выгрузки, без проверки новых сообщений и папок.')
    return True


def meme_record(chat,m,photo_index,day):
    src=Path(m['photos'][photo_index])
    dest=day_dir(day)/('meme'+src.suffix.lower())
    shutil.copyfile(src,dest)
    return {'id':m['id'],'path':str(dest),'url':'','chat':chat['chat'],
        'caption':'Из выгрузки за '+datetime.fromisoformat(m['date']).strftime('%d.%m')}


def select_meme(message_id, photo_index=0):
    d = day_dir()
    data = read_json(d/'telegram-exports.json')
    match = [(chat,m) for chat in data['chats'] for m in chat['messages'] if m['id']==message_id]
    if len(match)!=1 or match[0][0]['chat'].casefold()!=config()['telegram'].get('meme','').casefold():
        raise ValueError('Мем должен быть из заданного канала')
    chat,m = match[0]
    packet = read_json(d/'packet.json')
    packet['meme'] = meme_record(chat,m,photo_index,packet['date'])
    write_json(d/'packet.json',seal(packet))
    return packet['meme']
