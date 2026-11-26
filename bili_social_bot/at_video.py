"""视频评论区的 @ 邀请：持久化任务、共享观看结果、标准观后评论。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any
from bilibili_api.comment import CommentResourceType

from .bili_client import OutboundDeliveryUnknown, VideoItem
from .comment_reply_support import resolve_comment_position
from .conversation_understanding import comment_incoming_text, build_comment_source_context, build_analysis_reply_context
from .media import MediaAnalysis, _BilidownPending
from .state import BotState
from .utils import extract_bvid, now_iso, resolve_path, save_json_atomic


def is_video_invitation(row: dict, cfg: dict) -> bool:
    if not cfg.get('video_comment_enabled', False):
        return False
    item = row.get('item') or {}
    uri = str(item.get('uri') or '')
    native = str(item.get('native_uri') or '')
    is_video = bool(extract_bvid(uri) or extract_bvid(native)
                    or native.startswith('bilibili://video/')
                    or str(item.get('type') or '').lower() == 'video'
                    or item.get('business') == '视频')
    try:
        since = int(cfg.get('video_comment_since', 0) or 0)
        at_time = int(row.get('at_time') or row.get('ctime') or 0)
    except (ValueError, TypeError):
        return False
    # 安装时以前的通知保持旧回复行为，不能因新增功能批量重发旧视频评论。
    return is_video and (not since or at_time >= since)


class AtVideoQueue:
    """与聊天/主动评论状态分开落盘；每次修改都在文件锁内读取最新版本。"""
    def __init__(self, config):
        self.path = resolve_path(Path(config.state_file).with_name('at_video_tasks.json'))
        self.lock_state = BotState(str(self.path))

    def _load(self):
        if not self.path.exists():
            return {'jobs': {}}
        data = json.loads(self.path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or not isinstance(data.get('jobs'), dict):
            raise ValueError('@视频任务记录格式异常，停止发送')
        return data

    def enqueue(self, job):
        with self.lock_state._file_lock():
            data = self._load()
            if job['id'] in data['jobs']:
                return False
            data['jobs'][job['id']] = {**job, 'status': 'pending', 'phase': 'queued',
                                       'attempts': 0, 'created_at': now_iso()}
            save_json_atomic(self.path, data)
            return True

    def claim(self):
        with self.lock_state._file_lock():
            data = self._load()
            now = time.time()
            for job in data['jobs'].values():
                if job['status'] not in {'pending', 'working'}:
                    continue
                if float(job.get('lease_until', 0)) > now or float(job.get('retry_after', 0)) > now:
                    continue
                job.update(status='working', lease=uuid.uuid4().hex, lease_until=now+3600,
                           attempts=int(job.get('attempts', 0))+1)
                save_json_atomic(self.path, data)
                return copy.deepcopy(job)
        return None

    def update(self, job, **changes):
        with self.lock_state._file_lock():
            data = self._load()
            live = data['jobs'][job['id']]
            if live.get('lease') != job.get('lease'):
                raise RuntimeError('@视频任务租约已变更')
            live.update(changes, updated_at=now_iso())
            save_json_atomic(self.path, data)
            job.update(changes)


def standard_rule(config, item=None):
    """继承正在使用的日常评论媒体设置，独立评论不带邀请者的私人指令。"""
    rules = [r for r in config.comment_rules if r.get('enabled', False)]
    rule = copy.deepcopy(rules[0] if rules else {})
    rule.update(name='恋恋：应邀观看视频', comment_strategy='ai', max_comments_per_run=1)
    rule['video_source'] = {}
    rule['_at_video_wait_for_download'] = True
    rule['comment_templates'] = ['{comment_draft}']
    mu = rule.setdefault('media_understanding', {})
    mu.update(enabled=True, summarize=True, generate_comment=True, generate_share_reason=False)
    if item is not None:
        from .bot import _looks_touhou_video
        if not _looks_touhou_video(item):
            mu['vision_prompt'] = (
                '按时间顺序观察视频关键帧。具体描述人物或物体、环境、动作、画面文字、'
                '光线和场景变化；结合字幕和音频理解内容。只依据可见证据，'
                '非东方题材不要套用东方角色，不确定的人名不要猜。'
            )
            mu['character_vote_enabled'] = False
    return rule


def publisher_for(chat):
    """使用主评论状态与原发送管线，复用已登录客户端和模型实例。"""
    from .bot import SocialBot
    publisher = SocialBot.__new__(SocialBot)
    publisher.config = chat.config
    publisher.state = BotState(chat.config.state_file)
    publisher.client = chat.client
    publisher.ai = chat.ai
    publisher.media = chat.media
    publisher.persona_prompt = chat.persona_prompt
    publisher.owner_uid = chat.owner_uid
    publisher.owner_name = chat.owner_name
    publisher.knowledge_text = chat.knowledge_text
    publisher.touhou_learning = chat.learn_db
    publisher.touhou = chat.touhou
    publisher.emotes = chat.emotes
    publisher.stickers = chat.stickers
    publisher.morph_config = chat.morph_config
    publisher._comment_sent_ts = []
    publisher.log = chat.log
    return publisher


def has_pending_invitation(chat, item):
    path = resolve_path(Path(chat.config.state_file).with_name('at_video_tasks.json'))
    if not path.exists():
        return False
    aid, _, parent = resolve_comment_position(item)
    bvid = extract_bvid(str(item.get('uri') or item.get('native_uri') or ''))
    for job in AtVideoQueue(chat.config)._load()['jobs'].values():
        if job['status'] in {'pending','working'} and job['parent']==parent:
            if (aid and job['aid']==aid) or (bvid and job['bvid']==bvid):
                return True
    return False


def _mark_definite_reply_failure(chat,key,canon):
    # 通用 save 的词典序合并会保留新鲜 sending，必须在锁内明确结束本次失败占位。
    with chat.state._file_lock():
        data = chat.state._load_latest_unlocked()
        statuses = data.setdefault('interactive_replied',{})
        for k in (key,canon):
            if statuses.get(k) not in {'sent','unknown','blocked'}:
                statuses[k] = 'failed'
        save_json_atomic(chat.state.path,data)
        chat.state.data = data


async def enqueue_invitation(chat, row, cfg):
    from .chat_bot import _valid_incoming_text
    item = row.get('item') or {}
    user = row.get('user') or {}
    uid = str(user.get('mid') or '')
    at_id = str(row.get('id') or item.get('id') or item.get('source_id') or '')
    if not at_id or not uid or uid == str(chat.client.uid):
        return False
    key = f'at_notice:{at_id}'
    if chat._uid_blocked(uid) or chat._is_interactive_done(key):
        return False
    incoming = comment_incoming_text(item)
    if incoming and not _valid_incoming_text(incoming, cfg):
        chat.state.data['interactive_replied'][key] = 'ignored'
        chat.state.save()
        return False
    aid, root, parent = resolve_comment_position(item)
    uri = str(item.get('uri') or item.get('native_uri') or '')
    bvid = extract_bvid(uri) or extract_bvid(str(item.get('native_uri') or ''))
    if not bvid and aid:
        from bilibili_api.utils.aid_bvid_transformer import aid2bvid
        bvid = aid2bvid(aid)
    if not bvid or not root or not parent:
        chat.log(f'@视频邀请缺少有效视频/评论定位 at_id={at_id}', 'WARN')
        return False
    if not aid:
        meta = await chat.client.get_video_meta(bvid)
        aid = meta.aid
    canon = chat._comment_answered_key(aid or bvid, parent)
    if chat._is_interactive_done(canon):
        return False
    if await chat._send_admin_command_comment_reply(
        incoming=incoming, sender_uid=uid, key=key, canon=canon,
        oid=aid, root_id=root, parent_id=parent,
        resource_type=CommentResourceType.VIDEO,
        channel_label='at-notice', max_len=int(cfg.get('reply_max_length') or 900),
    ):
        return True
    if chat.config.dry_run:
        chat.log(f'[DRY-RUN] 将应邀观看视频并回复和发表评论 {bvid} at_id={at_id}')
        return True
    job = {'id': hashlib.sha256(key.encode()).hexdigest()[:24], 'key':key, 'canon':canon,
           'uid':uid, 'nickname':str(user.get('nickname') or user.get('uname') or '朋友'),
           'bvid':bvid, 'aid':aid, 'root':root, 'parent':parent,
           'row':row, 'incoming':incoming}
    added = AtVideoQueue(chat.config).enqueue(job)
    if added:
        chat._at_video_enqueued_round = int(getattr(chat,'_at_video_enqueued_round',0)) + 1
        chat.log(f'已加入@视频观看任务 {bvid} at_id={at_id}')
    return False  # 入队不等于已经发出回复。


def _actual_evidence(analysis):
    return bool(analysis and any(str(getattr(analysis, key, '') or '').strip() for key in
                ('vision_text','premium_vision_text','subtitle_text','audio_text','ai_conclusion_text')))


async def _run_job(chat, queue, job, cfg):
    if chat._uid_blocked(job['uid']):
        queue.update(job, status='blocked', reason='用户已屏蔽')
        return
    publisher = publisher_for(chat)
    if job.get('video') and job.get('analysis'):
        video = VideoItem(**job['video'])
        names = {f.name for f in fields(MediaAnalysis)}
        analysis = MediaAnalysis(**{k:v for k,v in job['analysis'].items() if k in names})
        rule = job['rule']
    else:
        video = await chat.client.get_video_meta(job['bvid'])
        if job['aid'] and video.aid != job['aid']:
            queue.update(job,status='blocked',reason='视频链接与评论区编号不一致')
            return
        allowed = publisher._filter_videos([video],{})
        allowed,_ = publisher.touhou.filter_videos(allowed)
        if not allowed:
            queue.update(job,status='blocked',reason='沿用日常视频内容筛选')
            return
        from .bot import blocked_by_comment_tags
        blocked,reason = await blocked_by_comment_tags(video,config=chat.config.data,
            fetch_tags=chat.client.get_video_tags,logger=chat.log)
        if blocked:
            queue.update(job,status='blocked',reason=f'沿用日常评论标签筛选:{reason}')
            return
        rule = standard_rule(chat.config, video)
        chat._learn_user_correction_if_any(job['incoming'], source='at-notice', video_id=video.bvid)
        analysis = await publisher._analyze_if_needed(video, rule)
        queue.update(job, video=asdict(video), analysis=asdict(analysis), rule=rule, phase='prepared')
    key, canon = job['key'], job['canon']
    # 崩溃可能发生在回复已落盘、任务阶段尚未保存之间：用聊天发送记录恢复。
    latest = BotState(chat.state.path)
    statuses = latest.data.get('interactive_replied') or {}
    known = [statuses.get(key), statuses.get(canon)]
    if any(x in {'blocked','ignored','invalid','old'} for x in known):
        queue.update(job,status='blocked',reason='原评论已被其他互动路径拦截')
        return
    if 'unknown' in known or any(str(x or '').startswith('sending:') for x in known):
        queue.update(job, status='unknown', reason='回复投递不确定，禁止自动重发')
        return
    replied = job.get('phase') == 'reply_sent' or 'sent' in known
    if not replied:
        row = job['row']
        source_ctx = build_comment_source_context(row.get('item') or {}, job['uid'], job['nickname'])
        img_ctx = await chat._comment_image_context(row.get('item') or {}, tag='@评论配图')
        context = (
            '场景：有人在这个视频的评论区@你，邀请你来观看。\n'
            '要求：用恋恋口吻先自然告诉对方这支视频主要讲了什么，再回应他本次的问题或感受。'
            '具体内容只能来自已获得的视频证据；不要把他的评论或配图当成视频事实。'
            '抽帧不等于看完，不说完整看过、从头看完；证据不足就如实说明。'
            '另有独立观后评论，不在这条回复里承诺已经发表。\n'
            + build_analysis_reply_context(analysis)
            + f'\n视频名称：{video.title[:220]}\n{source_ctx}{img_ctx}\n'
        )
        if _actual_evidence(analysis) and chat.ai.available():
            reply = await chat._generate_chat_reply(
                incoming_text=job['incoming'] or '（对方@你来看看这支视频）',
                context=context, max_len=int(cfg.get('reply_max_length') or 900),
                uid=job['uid'], use_premium=True)
        elif _actual_evidence(analysis):
            detail = analysis.summary or analysis.vision_text or analysis.subtitle_text or analysis.audio_text or analysis.premium_vision_text or analysis.ai_conclusion_text
            reply = '恋恋来啦～目前取得的内容是：' + str(detail)[:600]
        else:
            reply = '恋恋来啦～不过这次没能稳定取得视频内容，还不能准确告诉你它讲了什么。先不乱猜，等内容能读取时再来看呀。'
        if not reply.strip():
            raise RuntimeError('@视频回复未生成有效正文')
        reply = f"@{job['nickname']} " + reply.lstrip()
        ok, reason, safe = chat._finalize_reply(
            reply, channel='at', fake_item=video, analysis=analysis,
            max_len=int(cfg.get('reply_max_length') or 900), is_comment=True)
        if not ok:
            queue.update(job, status='blocked', reason=f'回复审核:{reason}')
            return
        if chat._uid_blocked(job['uid']):
            queue.update(job, status='blocked', reason='发送前用户已屏蔽')
            return
        if not chat.state.claim_interactive([key,canon]):
            queue.update(job, status='pending', retry_after=time.time()+600, lease_until=0)
            return
        try:
            result,suspicious = await chat.client.send_comment_reply_guarded(video.aid,job['root'],job['parent'],safe)
            if suspicious:
                for k in (key,canon): chat.state.data['interactive_replied'][k]='blocked'
                chat.state.save()
                queue.update(job,status='blocked',reason='回复发送预检拦截')
                return
        except (OutboundDeliveryUnknown, asyncio.TimeoutError):
            for k in (key,canon): chat.state.data['interactive_replied'][k]='unknown'
            chat.state.save()
            queue.update(job,status='unknown',reason='回复投递未知，不自动重试')
            return
        except Exception:
            _mark_definite_reply_failure(chat,key,canon)
            raise
        for k in (key,canon): chat.state.data['interactive_replied'][k]='sent'
        chat.state.record_history({'action':'at_reply','at_id':str(job['row'].get('id') or ''),
                                  'bvid':video.bvid,'aid':video.aid,'root_id':job['root'],
                                  'parent_id':job['parent'],'uid':job['uid'],'reply':safe,'video_summary_reply':True})
        chat.state.save()
        queue.update(job,phase='reply_sent')
        chat.log(f"已回复@视频内容 {video.bvid} uid={job['uid']}",'SUCCESS')
        if cfg.get('like_incoming_comment',True):
            try: await chat.client.like_comment(video.aid,job['parent'])
            except Exception as exc: chat.log(f'@评论点赞失败 {video.bvid}: {type(exc).__name__}','WARN')
    if not _actual_evidence(analysis):
        queue.update(job,status='blocked',reason='没有可靠视频证据，不发观后评论')
        return
    # 主动评论使用主状态：跨邀请、跨轮次、跨进程都不重复。
    publisher.state = BotState(chat.config.state_file)
    if publisher.state.data.get('comment_delivery_unknown',{}).get(video.bvid):
        queue.update(job,status='unknown',reason='视频已有投递未知记录')
        return
    if publisher.state.is_video_commented(rule['name'],video.bvid):
        queue.update(job,status='done',reason='视频已有机器人评论')
        return
    if job.get('phase') == 'root_sending':
        if publisher.state.is_video_commenting(video.bvid):
            publisher.state.mark_video_comment_unknown(rule['name'],video.bvid,
                {'action':'comment_delivery_unknown','rule':rule['name'],'bvid':video.bvid})
        queue.update(job,status='unknown',reason='上次在观后评论发送阶段中断，禁止自动重发')
        return
    queue.update(job,phase='root_sending')
    await publisher._execute_comment_rule(rule, preselected_items=[video],
                                         preanalyzed={video.bvid:analysis}, explicit_request=True)
    publisher.state.save()
    current = BotState(chat.config.state_file)
    if current.data.get('comment_delivery_unknown',{}).get(video.bvid):
        queue.update(job,status='unknown',reason='观后评论投递未知，不自动重发')
    elif current.is_video_commented(rule['name'],video.bvid):
        queue.update(job,status='done',reason='观后评论已发送')
        chat.log(f'已完成@视频观后评论 {video.bvid}', 'SUCCESS')
    else:
        relevant = [h for h in publisher.state.data.get('history',[]) if h.get('bvid')==video.bvid]
        last_action = relevant[-1].get('action') if relevant else ''
        if last_action == 'comment_failed':
            queue.update(job,phase='reply_sent')
            raise RuntimeError('观后评论明确发送失败')
        quota = current.can_comment_today(chat.config.daily_comment_limit)
        hourly = publisher._hourly_comment_budget(chat.config.data.get('rate_limits') or {})
        throttled = hourly > 0 and publisher._hourly_comment_sent() >= hourly
        degraded = getattr(chat.ai,'text_tier',lambda:'')() == 'deepseek'
        degraded_full = degraded and not current.can_comment_today(int(chat.config.data.get('degraded_comment_limit',20) or 20))
        if not quota or degraded_full or throttled or current.is_video_commenting(video.bvid):
            queue.update(job,status='pending',phase='reply_sent',retry_after=time.time()+600,lease_until=0,reason='等待评论额度或发送占位')
        else:
            queue.update(job,status='blocked',reason='沿用日常评论筛选或质量审核，未发表')


async def run_pending(chat, cfg):
    if chat.config.dry_run or not cfg.get('video_comment_enabled',False):
        return
    # 本地定时清理不调用模型；异常不阻止正常的观看和回复。
    try:
        from .bilidown_cleanup import OwnedDownloads
        removed=OwnedDownloads(chat.media.cache_dir,chat.config.data.get('media_understanding') or {}).cleanup()
        if removed:chat.log(f'Bilidown定时清理机器人旧视频 {len(removed)} 个', 'INFO')
    except Exception as exc:
        chat.log(f'Bilidown定时清理跳过: {type(exc).__name__}', 'WARN')
    queue = AtVideoQueue(chat.config)
    for _ in range(max(1,min(2,int(cfg.get('max_replies_per_run',2) or 2)))):
        job = queue.claim()
        if not job:
            return
        try:
            await _run_job(chat,queue,job,cfg)
        except _BilidownPending:
            queue.update(job, status='pending', lease_until=0, retry_after=time.time()+90,
                         pending_since=job.get('pending_since') or now_iso(),
                         reason='后台视频下载尚未完成，等待后续轮次继续观看')
            chat.log(f"@视频等待下载 {job['bvid']}，保留任务稍后继续", 'INFO')
        except asyncio.CancelledError:
            # 在途发送占位仍交给现有状态防重复；准备/分析阶段可在下次启动恢复。
            queue.update(job,status='pending',lease_until=0)
            raise
        except Exception as exc:
            failures = int(job.get('failures',0)) + 1
            terminal = failures >= 3
            queue.update(job,status='failed' if terminal else 'pending', lease_until=0,
                         failures=failures,retry_after=time.time()+600,reason=f'{type(exc).__name__}:{str(exc)[:160]}')
            chat.log(f"@视频任务失败 {job['bvid']}，{'停止自动重试' if terminal else '稍后重试'}: {type(exc).__name__}",'WARN')


def start_worker(chat,cfg):
    worker = getattr(chat,'_at_video_worker',None)
    if worker is not None and not worker.done():
        return
    chat._at_video_worker = asyncio.create_task(run_pending(chat,cfg))
    def done(task):
        if not task.cancelled() and task.exception():
            chat.log(f'@视频后台任务异常: {type(task.exception()).__name__}','ERROR')
    chat._at_video_worker.add_done_callback(done)
