"""Confirmed dates and a frozen story -> card chain; no historical catch-up."""
from datetime import date
import hashlib
import json
from pathlib import Path
import re

from lunar_python import Solar


def valid_date(month,day,calendar='solar'):
    if calendar=='solar':
        try:date(2000,month,day);return True
        except ValueError:return False
    return calendar=='lunar' and 1<=month<=12 and 1<=day<=30


def command(text,store,uid):
    text=text.strip()
    if text in ('取消生日祝福','关闭生日祝福','删除生日','不要生日祝福','拒收生日祝福'):
        store.cancel_birthday(uid)
        return '已关闭你的生日祝福。若想恢复，可以重新告诉我“我的生日是公历5月6日，请记住”。'
    if text=='查看生日':
        row=store.db.execute('SELECT * FROM birthdays WHERE uid=?',(str(uid),)).fetchone()
        if not row:return '还没有确认你的生日。可以说“我的生日是公历5月6日，请记住”，不用提供出生年份。'
        return ('记下的是'+('公历' if row['calendar']=='solar' else '农历')+
            f"{row['month']}月{row['day']}日，生日祝福"+('已开启。' if row['enabled'] else '已关闭。'))
    m=re.fullmatch(r'我的生日是(公历|农历)(\d{1,2})月(\d{1,2})日[，, ]*(?:请记住|记住了?)[。！!～~]*',text)
    if not m:return None
    month,day=int(m[2]),int(m[3]);calendar='solar' if m[1]=='公历' else 'lunar'
    if not valid_date(month,day,calendar):return '这个日期不太对，请确认月份和日期后再告诉我。'
    store.cancel_birthday(uid)
    store.set_birthday(uid,month,day,calendar,'user_confirmed')
    return f'记好啦，你的生日是{m[1]}{month}月{day}日。会在当天按接收设置送祝福，也可以随时说“取消生日祝福”。'


def due(row,day):
    if row['calendar']=='solar':return (day.month,day.day)==(row['month'],row['day'])
    lunar=Solar.fromYmd(day.year,day.month,day.day).getLunar()
    # An unspecified leap month must not produce two greetings in one year.
    return (lunar.getMonth(),lunar.getDay())==(row['month'],row['day'])


def stories_from_markdown(text):
    stories=[];current=None
    for line in text.splitlines():
        if line.strip().startswith('## '):
            if current and current['body'].strip():stories.append(current)
            current={'title':line.strip()[3:],'body':''}
        elif current is not None:current['body']+=(line.strip()+'\n')
    if current and current['body'].strip():stories.append(current)
    return stories


def chunks(text):
    parts=[];buf=''
    for char in text.strip():
        trial=buf+char
        if len(trial)>240 or len(trial.encode('utf-8'))>850 or len(json.dumps({'content':trial}).encode())>1850:
            if buf.strip():parts.append(buf.strip())
            buf=''
        buf+=char
        if len(buf)>140 and char in '。！？':parts.append(buf.strip());buf=''
    if buf.strip():parts.append(buf.strip())
    return parts


def schedule(service,now):
    cfg=service.cfg.get('birthdays',{})
    day=now.date();key=day.isoformat()
    if not cfg.get('enabled') or key<cfg['start_date']:return
    if not cfg.get('start_hour',10)<=now.hour<cfg.get('end_hour',22):return
    if service.store.get('scheduled_paused',False):return
    stories=service.birthday_stories
    if not stories:raise ValueError('birthday_stories_missing')
    for row in service.store.db.execute('SELECT * FROM birthdays WHERE enabled=1 ORDER BY uid'):
        uid=row['uid'];year=str(day.year)
        if not due(row,day) or not service.allowed(uid,'birthday'):continue
        prefix=f'birthday:{year}:{uid}'
        # Imported legacy receipt holds suppress even partial/ambiguous prior sends.
        if service.store.get('birthday_legacy_hold:'+year+':'+uid):continue
        if service.store.job(prefix+':opener'):continue
        index=int(hashlib.sha256((uid+year).encode()).hexdigest()[:8],16)%len(stories)
        story=stories[index];name=service.nickname(uid)
        bodies=[f'{name}，今天是你的生日呀～恋恋带来了一个小故事，讲完还有一张贺卡。愿新的一岁，有好多值得你期待的小事。']+chunks(story['body'])
        entries=[];previous=None
        for i,body in enumerate(bodies):
            identity=prefix+(':opener' if i==0 else ':part'+str(i))
            entries.append(dict(key=identity,uid=uid,kind='birthday',day=key,body=body,priority=4,
                payload={'parent':previous,'birthday_year':year},
                available=now.timestamp(),expires=now.replace(hour=cfg.get('end_hour',22),minute=0,second=0,microsecond=0).timestamp()))
            previous=identity
        if cfg.get('card_enabled',True):
            entries.append(dict(key=prefix+':card',uid=uid,kind='birthday_card',day=key,priority=4,
                payload={'parent':previous,'birthday_year':year},available=now.timestamp(),expires=entries[-1]['expires']))
        # One transaction freezes all parts and dependencies across restart.
        service.store.create_campaign('birthday:'+uid,year,entries)
        service.log('birthday_created',parts=len(entries))


def import_legacy(store,cache,sent,state,chat_state):
    counts={'confirmed_dates':0,'unconfirmed_skipped':0,'year_holds':0}
    for row in cache.get('users',[]):
        uid=str(row.get('mid',''));md=str(row.get('birthday_md') or '')
        m=re.fullmatch(r'(\d{2})-(\d{2})',md)
        if not uid.isdigit() or not m:continue
        # Only explicit normal/confirmed sources, never an absent confidence value.
        if row.get('birthday_confidence') not in ('normal','confirmed','user_confirmed'):
            counts['unconfirmed_skipped']+=1;continue
        month,day=map(int,m.groups())
        if not valid_date(month,day):continue
        if not store.db.execute('SELECT 1 FROM birthdays WHERE uid=?',(uid,)).fetchone():
            store.set_birthday(uid,month,day,'solar','legacy_profile_confirmed');counts['confirmed_dates']+=1
        if not store.user(uid)['name']:store.update_user(uid,row.get('uname',''))
    holds=set()
    for uid,value in sent.items():
        try:holds.add((str(date.fromisoformat(str(value)[:10]).year),str(uid)))
        except ValueError:continue
    for obj in (state,chat_state):
        for identity,status in obj.get('interactive_replied',{}).items():
            m=re.fullmatch(r'birthday_(?:part|card):(\d{4})-\d{2}-\d{2}:(\d+)(?::.*)?',identity)
            if m and (status in ('sent','unknown') or str(status).startswith('sending:')):holds.add(m.groups())
    for year,uid in holds:store.set('birthday_legacy_hold:'+year+':'+uid,True)
    counts['year_holds']=len(holds)
    return counts
