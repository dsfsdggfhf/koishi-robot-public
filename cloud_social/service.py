"""One account and one durable outbox for chat and scheduled greetings."""
import asyncio
from datetime import datetime,timedelta
import hashlib
import json
import logging
import mimetypes
from pathlib import Path
import random
import re
import time
import httpx
from zoneinfo import ZoneInfo

from bilibili_api import Credential,request_settings,session,user
from bilibili_api.utils.picture import Picture
from bili_social_bot.image_intent_helper import asks_about_previous_image, immediate_previous_user_image
from bili_social_bot.safety import validate_message
from .ai import RelayAI
from .calendar import Calendar
from .store import Store
from . import birthdays,preferences
from .dm_images import image_data_url, message_url

LOG=logging.getLogger('koishi.cloud')
TZ=ZoneInfo('Asia/Shanghai')
IMAGE_FALLBACK='这张图我暂时没看清，方便的话可以再发一张，或者告诉我你想问哪里呀～'
PROACTIVE_KINDS={'morning','nightly','event','birthday','birthday_card','announcement'}
ANNOUNCEMENT_LINK=re.compile(r'https://b23\.tv/[a-zA-Z0-9]{1,32}')


def validate_text(text):
    if not isinstance(text,str) or not text.strip():return False
    return len(text.encode('utf-8'))<=900 and len(json.dumps({'content':text}).encode('utf-8'))<=1900


def validate_announcement_text(body,link,forbidden_words):
    # Only an exact Bilibili short link from the operator's manifest is exempt.
    if (not isinstance(link,str) or not ANNOUNCEMENT_LINK.fullmatch(link)
            or not validate_text(body) or re.findall(r'(?i)https?://[^\s]+',body)!=[link]
            or body.count(link)!=1):return False
    ok,_,_=validate_message(body.replace(link,''),forbidden_words,marker='',require_marker=False)
    return ok


def risk_error(exc):
    code=str(getattr(exc,'code',''))
    text=str(exc).lower()
    return code in {'403','412','429','-101','-111','-509','-352','21026','21016'} or bool(re.search(r'(?<!\d)(?:HTTP\s*)?(?:403|412|429|-101|-111|-509|-352|21026|21016)(?!\d)',text,re.I)) or any(x in text for x in (
        'captcha','risk control','rate limit','too many requests','验证码','风控','过于频繁','操作频繁'))


def stamp(day,hour):
    return datetime.combine(day,datetime.min.time(),tzinfo=TZ).timestamp()+hour*3600


def optout_command(text):
    text=re.sub(r'\s+','',text).strip('。！!~～')
    if text in ('取消拒收','恢复接收','继续接收','接收通知'):return 'all_on'
    if text in ('拒收','拒收通知','拒收群发','不想收到','不想再收到了','别发了','别再发了','不要再发','不要再发了'):return 'all_off'
    if any(x in text for x in ('别忘','不要忘','不要关闭','不用停止')):return None
    negative=re.search(r'拒收|不要|别(?:再)?(?:给我)?发|关闭|取消|不想(?:再)?收|停止|不需要',text)
    if negative:
        if '早安' in text and any(s in text for s in ('晚安照常','晚安继续','保留晚安','晚安还要')):return 'morning_off'
        if '晚安' in text and any(s in text for s in ('早安照常','早安继续','保留早安','早安还要')):return 'night_off'
        if '早晚安' in text or ('早安' in text and '晚安' in text):return 'greetings_off'
        if '早安' in text or '早上问候' in text:return 'morning_off'
        if '晚安' in text or '晚上问候' in text:return 'night_off'
        if '节气' in text or '节日' in text:return 'events_off'
        if any(s in text for s in ('私信','联系我','回复我')):return 'hard_off'
        if any(s in text for s in ('通知','群发','问候')):return 'all_off'
    return None


class Service:
    def __init__(self,config_path):
        self.config_path=Path(config_path).resolve()
        self.cfg=json.loads(self.config_path.read_text(encoding='utf-8-sig'))
        if float(self.cfg.get('listen_since',0))<=0:raise ValueError('explicit_chat_cutover_time_required')
        datetime.fromisoformat(self.cfg['scheduled_start_date'])
        if len(self.cfg.get('providers',[]))!=2:raise ValueError('exactly_two_chat_providers_required')
        self.private=self.config_path.parent
        self.secrets=json.loads((self.private/'secrets.json').read_text(encoding='utf-8-sig'))
        self.store=Store(self.private/'state.sqlite3')
        self.store.import_seed(json.loads((self.private/'seed.json').read_text(encoding='utf-8-sig')))
        self.assets=Path(self.cfg['assets_dir'])
        def pool(name):return json.loads((self.assets/name).read_text(encoding='utf-8-sig'))
        self.pools={'morning':pool('_morning_full_pool.json')['items'],'nightly':pool('_night_final_pool_93.json')['items']}
        self.calendar=Calendar(pool('holiday_manifest.json'),pool('_holiday_pool.json'),pool('_solar_term_pool.json'))
        self.birthday_stories=[]
        if self.cfg.get('birthdays',{}).get('enabled'):
            self.birthday_stories=birthdays.stories_from_markdown(Path(self.cfg['birthdays']['stories_file']).read_text(encoding='utf-8-sig'))
            if not self.birthday_stories:raise ValueError('birthday_stories_missing')
        self.ai=RelayAI(self.cfg,self.secrets,self.store)
        cookies=self.secrets['bilibili_cookies']
        self.me=str(cookies.get('DedeUserID') or cookies.get('dedeuserid'))
        self.credential=Credential(sessdata=cookies.get('SESSDATA') or cookies.get('sessdata'),
            bili_jct=cookies.get('bili_jct'),dedeuserid=self.me,buvid3=cookies.get('buvid3'),buvid4=cookies.get('buvid4'))
        request_settings.set_wbi_retry_times(1)
        self.api_lock=asyncio.Lock();self.last_read=0

    def log(self,event,**counts):
        # No raw messages, recipients, URLs, API bodies, or credentials in journalctl.
        LOG.info('%s %s',event,json.dumps(counts,ensure_ascii=False))

    def pause(self,rejected_job=None):
        now=time.time();prior=self.store.get('risk_state',{})
        strikes=min(5,prior.get('strikes',0)+1) if now-prior.get('at',0)<86400 else 1
        until=max(now+min(7200,600*2**(strikes-1)),self.store.get('pause_until',0))
        self.store.set('pause_until',until)
        self.store.set('risk_state',{'at':now,'strikes':strikes})
        if rejected_job is not None:
            self.store.update_job(rejected_job,status='queued',available=until)
        self.store.defer_dm_expiry(now,until)
        self.log('platform_pause',seconds=int(until-now))

    async def read_api(self,fn):
        async with self.api_lock:
            await asyncio.sleep(max(0,1.5-(time.monotonic()-self.last_read)))
            if self.store.get('pause_until',0)>time.time():
                raise RuntimeError('platform_cooldown_active')
            try:return await asyncio.wait_for(fn(),35)
            except Exception as exc:
                self.store.set('platform_last_error',{'at':time.time(),'type':type(exc).__name__,
                    'code':getattr(exc,'code',None),'http':getattr(getattr(exc,'response',None),'status_code',None)})
                if risk_error(exc):self.pause()
                raise
            finally:self.last_read=time.monotonic()

    async def upload_picture(self,path):
        # Preserve the existing bot's raw multipart workaround; SDK 17.4.2 has
        # a known BiliAPIFile.read incompatibility in Picture.upload.
        if self.send_held():raise RuntimeError('outbound_send_hold_active')
        path=Path(path)
        if path.stat().st_size>4*1024*1024:raise ValueError('sticker_file_too_large')
        cookies=self.secrets['bilibili_cookies']
        async with httpx.AsyncClient(timeout=45,trust_env=False,
                headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/'}) as client:
            response=await client.post('https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs',
                data={'biz':'draw','category':'daily','csrf':cookies['bili_jct']},
                files={'file_up':(path.name,path.read_bytes(),mimetypes.guess_type(path.name)[0] or 'image/jpeg')},cookies=cookies)
        response.raise_for_status()
        result=response.json()
        if int(result.get('code') or 0):
            error=RuntimeError('sticker_upload_rejected');error.code=result['code'];raise error
        data=result.get('data') or {};url=data.get('image_url') or data.get('url')
        if not url:raise ValueError('sticker_upload_missing_url')
        return Picture(url=url,width=int(data.get('image_width') or 0),height=int(data.get('image_height') or 0),
            size=float(data.get('img_size') or data.get('size') or 0),imageType=path.suffix.lstrip('.'))

    def allowed(self,uid,kind):
        flags=self.store.user(uid)['flags']
        if flags.get('hard_blocked'):return False
        if kind in PROACTIVE_KINDS and flags.get('no_proactive'):return False
        if kind=='sticker' and flags.get('no_stickers'):return False
        if kind in ('birthday','birthday_card'):
            if flags.get('no_birthday'):return False
            row=self.store.db.execute('SELECT enabled FROM birthdays WHERE uid=?',(str(uid),)).fetchone()
            if not row or not row[0]:return False
        if kind in ('morning','nightly','event'):
            if not flags.get('greeting_member'):return False
            if kind=='morning' and flags.get('no_morning'):return False
            if kind=='nightly' and flags.get('no_nightly'):return False
            if kind=='event' and flags.get('no_events'):return False
        return True

    def inbox_fresh(self,now,uid=None):
        authenticated=self.store.get('authentication_verified_at',0)
        if not authenticated:return True  # Unit-test instances do not authenticate.
        polled=self.store.get('last_poll_ok',0)
        if uid is not None:polled=min(polled,self.store.get('last_poll_uid:'+str(uid),0))
        return polled>=max(authenticated,self.store.get('pause_until',0)) and now-polled<=max(120,self.cfg.get('poll_seconds',45)*3)

    def send_held(self,kind=None):
        return (self.store.get('restore_send_hold',False) or self.store.get('emergency_send_hold',False)
                or (kind in PROACTIVE_KINDS and self.store.get('scheduled_paused',False)))

    def ready_for_dispatch(self,job,now):
        current=self.store.job(job['id'])
        if not current or current['status']!='queued':return False
        if current['expires']<=now:
            self.store.update_job(job['id'],status='expired');return False
        if not self.allowed(job['uid'],job['kind']):
            self.store.update_job(job['id'],status='blocked');return False
        if (self.send_held(job['kind']) or self.store.get('pause_until',0)>now
                or not self.inbox_fresh(now,job['uid'])):
            self.store.update_job(job['id'],available=max(current['available'],now+15));return False
        return True

    def maintain_private_data(self,now=None):
        now=time.time() if now is None else now
        day=datetime.fromtimestamp(now,TZ).date().isoformat()
        if self.store.get('last_backup_day')!=day and not self.cfg.get('encrypted_backup',{}).get('enabled'):
            self.store.backup(self.private/'backups'/('state_'+day+'.sqlite3'))
            self.store.set('last_backup_day',day)
        retention=float(self.cfg.get('message_body_retention_days',0) or 0)
        if retention>0 and self.store.get('last_body_retention_day')!=day:
            self.store.expire_message_bodies(now-retention*86400)
            self.store.set('last_body_retention_day',day)

    def greeting(self,kind,uid,day):
        pool=self.pools[kind]
        # Deterministic daily choice survives crashes without changing the payload.
        ordinal=datetime.fromisoformat(day).toordinal()
        offset=int(hashlib.sha256(str(uid).encode()).hexdigest()[:8],16)
        template=pool[(ordinal+offset)%len(pool)]['msg']
        return template.format(nickname=self.nickname(uid))

    def nickname(self,uid):
        name=self.store.user(uid)['name'] or '朋友'
        return re.sub(r'[\r\n\x00-\x1f]','',name)[:24]

    def schedule(self,now=None):
        now=now or datetime.now(TZ)
        day=now.date();key=day.isoformat();ts=now.timestamp()
        if key<self.cfg['scheduled_start_date'] or self.store.get('scheduled_paused',False) or self.send_held():return
        birthdays.schedule(self,now)
        for kind in ('morning','nightly'):
            options=self.cfg[kind]
            if not options['start_hour']<=now.hour<options['end_hour']:continue
            if not self.store.campaign(kind,key):
                entries=[]
                for row in self.store.db.execute('SELECT uid FROM users ORDER BY uid'):
                    uid=row[0]
                    if not self.allowed(uid,kind):continue
                    body=self.greeting(kind,uid,key)
                    if not validate_text(body):continue
                    entries.append(dict(key=f'{kind}:{key}:{uid}',uid=uid,kind=kind,day=key,
                        body=body,priority=8,expires=stamp(day,options['end_hour'])))
                self.store.create_campaign(kind,key,entries)
                self.log('campaign_created',kind=kind,count=len(entries))
            self.store.finish_campaign(kind,key,ts)
        morning=self.store.campaign('morning',key)
        if not morning or morning['done_at'] is None:return
        if ts<morning['done_at']+180 or now.hour>=self.cfg.get('event_end_hour',14):return
        if self.store.campaign('event',key):return
        entries=[]
        for uid in self.store.sent_users('morning',key):
            if not self.allowed(uid,'event'):continue
            selection=self.calendar.choose(day,self.store.user(uid)['flags'])
            if not selection:continue
            event_id,template=selection
            body=template.format(nickname=self.nickname(uid))
            if not validate_text(body):continue
            entries.append(dict(key=f'event:{key}:{uid}',uid=uid,kind='event',day=key,
                payload={'event_id':event_id},body=body,priority=9,available=morning['done_at']+180,
                expires=stamp(day,self.cfg.get('event_end_hour',14))))
        self.store.create_campaign('event',key,entries)
        self.log('event_campaign_created',count=len(entries),delay_seconds=180)

    async def poll(self):
        sessions=await self.read_api(lambda:session.get_sessions(self.credential,session_type=1))
        rows=list(sessions.get('session_list') or [])
        # Always inspect recent sessions and rotate the rest, including quiet old contacts.
        cursor=int(self.store.get('session_cursor',0))
        tail=rows[4:];rotating=[tail[(cursor+i)%len(tail)] for i in range(min(4,len(tail)))] if tail else []
        pending=self.store.pending_recipients(time.time())
        by_uid={str(row.get('talker_id')):row for row in rows if str(row.get('session_type',1))=='1'}
        rows=[by_uid.get(uid,{'talker_id':uid}) for uid in pending]+rows[:4]+rotating
        self.store.set('session_cursor',cursor+4)
        seen=set()
        for row in rows:
            uid=str(row.get('talker_id') or '')
            if uid in seen:continue
            seen.add(uid)
            if not uid.isdigit() or int(uid)<=0 or str(row.get('session_type',1))!='1' or uid in {self.me,'844424930131966'} or not self.allowed(uid,'dm'):continue
            last=row.get('last_msg') or {}
            mark=str(last.get('msg_key') or last.get('msg_seqno') or row.get('session_ts') or '')
            if mark and self.store.get('session_mark:'+uid)==mark:
                self.store.set('last_poll_uid:'+uid,time.time());continue
            result=await self.read_api(lambda:session.fetch_session_msgs(int(uid),self.credential,begin_seqno=0))
            messages=sorted(result.get('messages') or [],key=lambda m:int(m.get('msg_seqno') or 0))
            # Record current history locally; only post-cutover inbound messages become jobs.
            for message in messages:
                # Platform/system/withdrawal events are not user conversation turns.
                if str(message.get('msg_type')) not in {'1','2'}:continue
                if str(message.get('sender_uid')) not in {uid,self.me}:continue
                text=self.store.add_message(uid,message)
                if str(message.get('sender_uid'))!=uid:continue
                received=float(message.get('timestamp') or 0)
                if received<self.cfg['listen_since']:continue
                new_image_flow=received>=self.cfg.get('dm_images_since',0)
                if not new_image_flow and str(message.get('msg_type'))=='2':continue
                identity=str(message.get('msg_key') or message.get('msg_seqno'))
                key=f'dm:{uid}:{identity}'
                if self.store.job(key):continue
                command=optout_command(text) if str(message.get('msg_type'))=='1' else None
                body=''
                if command:
                    if command=='hard_off':
                        self.store.update_user(uid,hard_blocked=True)
                        # Respect an explicit request for no further private messages.
                        self.store.enqueue(key,uid,'dm',datetime.now(TZ).date().isoformat())
                        self.store.update_job(key,status='blocked')
                        continue
                    if command=='all_on':self.store.update_user(uid,no_proactive=False,no_morning=False,no_nightly=False,no_events=False)
                    elif command=='all_off':self.store.update_user(uid,no_proactive=True)
                    elif command=='morning_off':self.store.update_user(uid,no_morning=True)
                    elif command=='night_off':self.store.update_user(uid,no_nightly=True)
                    elif command=='events_off':self.store.update_user(uid,no_events=True)
                    elif command=='greetings_off':self.store.update_user(uid,no_morning=True,no_nightly=True)
                    body='记好啦，这类主动问候就先停下。你来找恋恋聊天时，还是可以正常说话的～' if command!='all_on' else '好呀，已经恢复接收问候啦～之后不想收时，再告诉我就好。'
                elif str(message.get('msg_type'))=='1':
                    body=preferences.command(text,self.store,uid) or birthdays.command(text,self.store,uid) or ''
                managed=bool(body) and not command
                payload={'seq':int(message.get('msg_seqno') or 0),'incoming':text,'received':received,
                         'no_sticker':managed or bool(command)}
                if not command and not body:
                    if new_image_flow and asks_about_previous_image(text):
                        image=message if str(message.get('msg_type'))=='2' else immediate_previous_user_image(messages,message,uid)
                        url=message_url(image)
                        if url:
                            payload.update(image_url=url,no_sticker=True)
                        elif image:
                            body=IMAGE_FALLBACK
                            payload['no_sticker']=True
                    if not body and not payload.get('image_url') and str(message.get('msg_type'))=='2':
                        body='收到图啦～'
                        payload.update(reaction=True,no_sticker=True)
                    elif not body and not text.strip():
                        body='我收到啦～这条消息暂时读不了，可以用文字告诉恋恋想聊什么呀。'
                payload['mergeable']=bool(str(message.get('msg_type'))=='1' and text.strip()
                    and len(text)<=400 and not body and not command and not payload.get('image_url'))
                self.store.enqueue(key,uid,'dm',datetime.now(TZ).date().isoformat(),
                    payload=payload,body=body,
                    priority=1,expires=time.time()+7200)
            if mark:self.store.set('session_mark:'+uid,mark)
            self.store.set('last_poll_uid:'+uid,time.time())
        self.store.set('last_poll_ok',time.time())

    async def send_one(self):
        now=time.time()
        if self.send_held():return
        if self.store.get('pause_until',0)>now:return
        if self.store.get('outbound_after',0)>now:return
        day=datetime.fromtimestamp(now,TZ).date().isoformat()
        cap=max(1,int(self.cfg.get('daily_outbound_attempt_limit',1800)))
        usage=self.store.get('outbound_day_attempts',{})
        if usage.get('day')==day and int(usage.get('count',0))>=cap:return
        if not self.inbox_fresh(now):return
        job=self.store.next_job(now)
        if not job:return
        uid=job['uid'];kind=job['kind'];payload=json.loads(job['payload'])
        if self.send_held(kind):
            self.store.update_job(job['id'],available=now+15);return
        if not self.allowed(uid,kind):
            self.store.update_job(job['id'],status='blocked');return
        parent_id=payload.get('parent')
        if parent_id and kind in ('birthday','birthday_card'):
            parent=self.store.job(parent_id)
            if not parent or parent['status'] in ('blocked','unknown','failed','expired'):
                self.store.update_job(job['id'],status='blocked');return
            if parent['status']!='sent':
                self.store.update_job(job['id'],available=now+20);return
        if kind=='announcement' and not self.ready_for_dispatch(job,now):return
        if kind in PROACTIVE_KINDS:
            try:
                relation=await self.read_api(lambda:user.User(int(uid),credential=self.credential).get_relation())
                rel=(relation.get('relation') or {}).get('attribute',0)
                back=(relation.get('be_relation') or {}).get('attribute',0)
                if not (int(rel)&2 and int(back)&4):
                    self.store.update_job(job['id'],status='blocked');return
            except Exception:
                self.store.update_job(job['id'],available=time.time()+300);return
        if not self.ready_for_dispatch(job,time.time()):return
        job=self.store.coalesce_dm(job,time.time(),window=float(self.cfg.get('dm_merge_seconds',45)),
            max_items=max(1,int(self.cfg.get('dm_merge_max_messages',6))))
        payload=json.loads(job['payload'])
        body=job['body']
        if (kind=='dm' and body and job['provider'] and 'draft_inbox_seq' not in payload
                and self.store.latest_inbound_seq(uid)>payload.get('seq',0)):
            body=''
            self.store.update_job(job['id'],body='',provider=None)
        if kind=='dm' and not body:
            allowed,next_at=self.store.claim_user_request(uid,time.time(),
                limit=self.cfg.get('user_request_limit',10),period=self.cfg.get('user_request_window_seconds',600))
            if not allowed:
                self.store.update_job(job['id'],available=next_at);return
            self.store.update_job(job['id'],status='generating')
            try:
                history=self.store.context(uid,payload['seq'],self.me)
                revision=self.store.get('preference_revision:'+uid,0)
                inbound_seq=self.store.latest_inbound_seq(uid)
                flags=dict(self.store.user(uid)['flags'],saved_preferences=preferences.context(self.store,uid))
                history=history or [{'role':'user','content':payload['incoming']}]
                if payload.get('image_url'):
                    body,provider=IMAGE_FALLBACK,None
                    if self.cfg.get('vision_provider') and not job['attempts']:
                        # Claim once before any paid call so restart or ambiguous timeout never repeats it.
                        self.store.update_job(job['id'],attempts=1)
                        try:
                            image=await asyncio.wait_for(image_data_url(payload['image_url']),15)
                            if self.allowed(uid,kind) and self.store.get('pause_until',0)<=time.time():
                                history[-1]={'role':'user','content':[
                                    {'type':'text','text':payload['incoming'][:400]},
                                    {'type':'image_url','image_url':{'url':image}}]}
                                body,provider=await self.ai.reply(uid,history,flags,vision=True)
                        except Exception as exc:
                            self.log('dm_vision_unavailable',category=type(exc).__name__)
                else:
                    body,provider=await self.ai.reply(uid,history,flags)
                if (self.store.get('preference_revision:'+uid,0)!=revision
                        or self.store.latest_inbound_seq(uid)>inbound_seq):
                    self.store.update_job(job['id'],status='queued',body='',provider=None);return
                payload['draft_inbox_seq']=inbound_seq
                payload['draft_preference_revision']=revision
                self.store.update_job(job['id'],body=body,provider=provider,status='queued',payload=json.dumps(payload,ensure_ascii=False))
            except Exception:
                attempts=job['attempts']+1
                self.store.update_job(job['id'],status='failed' if attempts>=3 else 'queued',attempts=attempts,available=time.time()+180)
                self.log('chat_generation_unavailable',attempt=attempts);return
        if kind=='announcement':
            if not validate_announcement_text(body,payload.get('approved_link'),self.cfg.get('forbidden_words',[])):
                self.store.update_job(job['id'],status='blocked');self.log('message_safety_or_length_blocked');return
        elif kind not in ('sticker','birthday_card'):
            ok,_,safe=validate_message(body,self.cfg.get('forbidden_words',[]),marker='',require_marker=False)
            if not ok or not validate_text(safe):
                self.store.update_job(job['id'],status='blocked');self.log('message_safety_or_length_blocked');return
            body=safe
            self.store.update_job(job['id'],body=body)
        if not self.allowed(uid,kind):
            self.store.update_job(job['id'],status='blocked');return
        if not self.ready_for_dispatch(job,time.time()):return
        picture=None
        if kind=='dm' and payload.get('reaction') and self.allowed(uid,'sticker'):
            pictures=[path for path in Path(self.cfg['stickers_dir']).glob('*')
                      if path.is_file() and path.suffix.lower() in ('.png','.jpg','.jpeg','.webp','.gif')]
            if pictures:
                try:picture=await self.upload_picture(random.choice(pictures))
                except Exception as exc:
                    if risk_error(exc):self.pause();return
        if kind=='birthday_card':
            try:
                path=await self.prepare_birthday_card(job)
                if not self.ready_for_dispatch(job,time.time()):return
                picture=await self.upload_picture(path)
            except Exception as exc:
                if risk_error(exc):self.pause()
                attempts=job['attempts']+1
                self.store.update_job(job['id'],status='failed' if attempts>=3 else 'queued',attempts=attempts,available=time.time()+300)
                self.log('birthday_card_prepare_failed',category=type(exc).__name__);return
        if kind=='sticker':
            parent=self.store.job(payload['parent'])
            if not parent or parent['status']!='sent':
                self.store.update_job(job['id'],status='blocked');return
            try:picture=await self.upload_picture(payload['file'])
            except Exception as exc:
                if risk_error(exc):self.pause()
                self.store.update_job(job['id'],status='failed');return
        # Recheck pause/opt-out after long model generation or picture upload.
        if kind=='dm' and picture and not self.allowed(uid,'sticker'):picture=None
        if not self.allowed(uid,kind):
            self.store.update_job(job['id'],status='blocked');return
        if (payload.get('draft_inbox_seq') is not None
                and (self.store.latest_inbound_seq(uid)>payload['draft_inbox_seq']
                     or self.store.get('preference_revision:'+uid,0)!=payload.get('draft_preference_revision',0))):
            self.store.update_job(job['id'],body='',provider=None);return
        current=self.store.job(job['id'])
        if not current or current['body']!=body:return
        if not self.ready_for_dispatch(job,time.time()):return
        # The sender loop has one worker. Write the attempt and dispatch state
        # before yielding so crashes or timeouts cannot reset the daily cap.
        day=datetime.now(TZ).date().isoformat()
        usage=self.store.get('outbound_day_attempts',{})
        attempts=int(usage.get('count',0)) if usage.get('day')==day else 0
        if attempts>=cap:return
        self.store.set('outbound_day_attempts',{'day':day,'count':attempts+1})
        self.store.update_job(job['id'],status='dispatching')
        try:
            await asyncio.wait_for(session.send_msg(self.credential,int(uid),
                session.EventType.PICTURE if picture else session.EventType.TEXT,picture or body),60)
        except BaseException as exc:
            code=str(getattr(exc,'code',''))
            if risk_error(exc):
                # A definite rejection is safe to retry later; network ambiguity is not.
                self.pause(rejected_job=job['id'] if code else None)
                if not code:self.store.update_job(job['id'],status='unknown')
            elif code in {'21047','21007','12002','-400'}:
                self.store.update_job(job['id'],status='blocked')
                if code=='21047':self.store.update_user(uid,no_proactive=True)
            else:self.store.update_job(job['id'],status='unknown')
            self.log('outbound_not_confirmed',category=type(exc).__name__,code=code)
            if isinstance(exc,asyncio.CancelledError):raise
        else:
            self.store.update_job(job['id'],status='sent',sent_at=time.time())
            self.log('outbound_confirmed',kind=kind)
            if kind=='dm' and payload.get('incoming') and not payload.get('no_sticker'):
                pictures=list(Path(self.cfg['stickers_dir']).glob('*'))
                if pictures and (('表情包' in payload['incoming']) or random.random()<self.cfg.get('sticker_probability',0.15)):
                    self.store.enqueue(job['id']+':sticker',uid,'sticker',job['day'],
                        payload={'parent':job['id'],'file':str(random.choice(pictures))},priority=3,expires=time.time()+1200)
        finally:
            lo,hi=self.cfg.get('send_gap_seconds',[10,20])
            self.store.set('outbound_after',time.time()+random.uniform(max(10,lo),max(20,hi)))

    async def prepare_birthday_card(self,job):
        from bili_social_bot.birthday_card import render_card,DEFAULT_WISHES
        cfg=self.cfg['birthdays'];folder=self.private/'birthday_cards'
        folder.mkdir(exist_ok=True)
        out=folder/(job['day']+'_'+job['uid']+'.jpg')
        if out.exists():return out
        avatar=None
        try:
            info=await self.read_api(lambda:user.User(int(job['uid']),credential=self.credential).get_user_info())
            url=info.get('face','').replace('http://','https://',1)
            from urllib.parse import urlsplit
            host=urlsplit(url).hostname or ''
            if url.startswith('https://') and host.endswith('.hdslb.com'):
                async with httpx.AsyncClient(timeout=15,trust_env=False) as client:
                    async with client.stream('GET',url) as response:
                        response.raise_for_status();data=bytearray()
                        async for block in response.aiter_bytes():
                            data.extend(block)
                            if len(data)>4*1024*1024:raise ValueError('avatar_too_large')
                avatar=folder/('avatar_'+job['uid']+'.jpg');avatar.write_bytes(data)
        except Exception as exc:
            if risk_error(exc):raise
        index=int(hashlib.sha256((job['uid']+job['day']).encode()).hexdigest()[:8],16)
        await asyncio.to_thread(render_card,nickname=self.nickname(job['uid']),wish=DEFAULT_WISHES[index%len(DEFAULT_WISHES)],
            variant='abcde'[index%5],avatar_path=avatar,datestr=job['day'],art=cfg['art_file'],sticker=cfg['art_file'],out=out)
        return out

    async def run(self):
        self.store.recover()
        # A restart must not turn an account's cooldown into fresh API requests.
        while self.store.get('pause_until',0)>time.time():
            self.store.set('heartbeat',time.time())
            await asyncio.sleep(min(30,self.store.get('pause_until')-time.time()))
        info=await self.read_api(lambda:user.get_self_info(self.credential))
        if str(info.get('mid'))!=self.me:raise RuntimeError('cloud_account_mismatch')
        self.store.set('authentication_verified_at',time.time())
        self.log('service_started',recipients=self.store.db.execute('SELECT COUNT(*) FROM users').fetchone()[0])
        async def schedule_loop():
            while True:
                self.schedule()
                self.maintain_private_data()
                self.store.set('heartbeat',time.time())
                await asyncio.sleep(15)
        async def poll_loop():
            while True:
                if self.store.get('pause_until',0)<=time.time():
                    try:await self.poll()
                    except Exception as exc:
                        self.store.set('last_poll_error',{'at':time.time(),'type':type(exc).__name__})
                        self.log('private_poll_failed',category=type(exc).__name__)
                await asyncio.sleep(max(90,self.cfg.get('poll_seconds',90)))
        async def send_loop():
            while True:
                try:await self.send_one()
                except Exception as exc:self.log('worker_error',category=type(exc).__name__)
                await asyncio.sleep(2)
        await asyncio.gather(schedule_loop(),poll_loop(),send_loop())
