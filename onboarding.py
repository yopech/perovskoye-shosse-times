"""Personalize non-secret settings; source credentials use the separate setup menu."""
from common import ROOT,config,write_json


def personalize():
    cfg=config()
    print('Настройки газеты. Enter — оставить текущее значение, «-» — очистить список/адрес.')
    for label,container,key in (
        ('Название газеты',cfg,'title'),
        ('Город для мероприятий',cfg['interests'],'city'),
        ('Твой личный Gmail (если нужен)',cfg['personal_mail'],'address'),
        ('Папки Telegram, через запятую',cfg['telegram'],'folders'),
        ('Чаты для сводки, через запятую',cfg['telegram'],'digest'),
        ('Каналы бизнес-встреч, через запятую',cfg['telegram'],'business'),
        ('Каналы пробежек, через запятую',cfg['telegram'],'running'),
        ('Каналы выставок, через запятую',cfg['telegram'],'exhibitions'),
        ('Канал для мема дня, точное название',cfg['telegram'],'meme')):
        current=container[key]
        visible=', '.join(current) if isinstance(current,list) else current
        value=input(f'{label} [{visible}]: ').strip()
        if not value:
            continue
        if isinstance(current,list):
            container[key]=[] if value=='-' else [v.strip() for v in value.split(',') if v.strip()]
        else:
            if key in ('title','city') and value=='-':
                raise ValueError('Название и город не должны быть пустыми')
            if key=='address' and value!='-' and ('@' not in value or any(c.isspace() for c in value)):
                raise ValueError('Нужен полный адрес почты')
            container[key]='' if value=='-' else value
    write_json(ROOT/'config.json',cfg)
    return {'saved':True,'printing_enabled':cfg['print_enabled']}
