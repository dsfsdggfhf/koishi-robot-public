"""Offline delivery checks: no credentials, real model calls or platform writes."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from .service import Service
from .store import Store


class DeliveryHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'state.sqlite3');self.addCleanup(self.store.db.close)
        self.svc=Service.__new__(Service)
        self.svc.store=self.store;self.svc.me='999';self.svc.credential=object()
        self.svc.private=self.root
        self.svc.cfg={'listen_since':time.time()-600,'stickers_dir':str(self.root),
                      'send_gap_seconds':[10,20],'poll_seconds':90,'sticker_probability':0}
        self.svc.log=lambda *a,**k:None
        self.svc.ai=type('AI',(),{'reply':AsyncMock(return_value=('窗边的花盆整理好了。','mock'))})()
        self.svc.upload_picture=AsyncMock(return_value=object())
        async def read(fn):return await fn()
        self.svc.read_api=read
        self.store.update_user('101',greeting_member=True)

    def message(self,seq,text,kind=1,uid='101',age=0):
        data={'url':'https://message.biliimg.com/bfs/im/mock.jpg'} if kind==2 else {'content':text}
        return {'msg_key':str(seq),'msg_seqno':seq,'sender_uid':uid,'msg_type':kind,
                'content':json.dumps(data),'timestamp':time.time()-age}

    async def poll(self,*messages,rows=None):
        with patch('cloud_social.service.session.get_sessions',AsyncMock(return_value={
                'session_list':rows if rows is not None else [{'talker_id':'101'}]})), \
             patch('cloud_social.service.session.fetch_session_msgs',AsyncMock(return_value={'messages':messages})) as fetch:
            await self.svc.poll()
        return fetch

    def enqueue(self,key='job',uid='101',kind='dm',body='已经准备好的文字。',**kwargs):
        self.store.enqueue(key,uid,kind,'d',body=body,**kwargs)
        return self.store.job(key)

    async def test_restore_and_emergency_hold_stop_every_outbound_kind(self):
        for flag in ('restore_send_hold','emergency_send_hold'):
            with self.subTest(flag=flag):
                self.store.set(flag,True)
                self.enqueue(key=flag)
                with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
                    await self.svc.send_one();send.assert_not_awaited()
                self.store.set(flag,False)
        self.svc.ai.reply.assert_not_awaited()

    async def test_scheduled_pause_retains_existing_proactive_queue(self):
        for kind in ('morning','nightly','event','birthday','birthday_card'):
            with self.subTest(kind=kind):
                job=self.enqueue(key=kind,kind=kind)
                self.store.set('scheduled_paused',True)
                with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
                    await self.svc.send_one();send.assert_not_awaited()
                self.assertEqual(self.store.job(job['id'])['status'],'queued')
        self.svc.ai.reply.assert_not_awaited()

    async def test_scheduled_pause_does_not_stop_requested_chat(self):
        self.store.set('scheduled_paused',True);self.enqueue()
        with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
            await self.svc.send_one();send.assert_awaited_once()

    async def test_final_check_catches_hold_or_optout_during_generation(self):
        for change in ('restore','emergency','optout'):
            with self.subTest(change=change):
                key='during-'+change
                self.enqueue(key=key,body='',payload={'seq':1,'incoming':'花盆好看吗？'})
                async def reply(*args,**kwargs):
                    if change=='optout':self.store.update_user('101',hard_blocked=True)
                    else:self.store.set('restore_send_hold' if change=='restore' else 'emergency_send_hold',True)
                    return '花盆放在窗边。','mock'
                self.svc.ai.reply.side_effect=reply
                with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
                    await self.svc.send_one();send.assert_not_awaited()
                self.store.update_user('101',hard_blocked=False)
                self.store.set('restore_send_hold',False);self.store.set('emergency_send_hold',False)
                self.store.update_job(key,status='blocked')

    async def test_final_expiry_check_after_model_generation(self):
        self.enqueue(body='',payload={'seq':1,'incoming':'花盆好看吗？'})
        async def reply(*args,**kwargs):
            self.store.db.execute("UPDATE jobs SET expires=0 WHERE id='job'");self.store.db.commit()
            return '花盆放在窗边。','mock'
        self.svc.ai.reply.side_effect=reply
        with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
            await self.svc.send_one();send.assert_not_awaited()
        self.assertEqual(self.store.job('job')['status'],'expired')

    async def test_final_expiry_check_after_picture_upload(self):
        self.enqueue(payload={'reaction':True})
        (self.root/'sticker.png').write_bytes(b'mocked picture')
        async def upload(*args):
            self.store.db.execute("UPDATE jobs SET expires=0 WHERE id='job'");self.store.db.commit()
            return object()
        self.svc.upload_picture.side_effect=upload
        with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
            await self.svc.send_one();send.assert_not_awaited()
        self.assertEqual(self.store.job('job')['status'],'expired')

    async def test_preference_change_during_upload_invalidates_generated_body(self):
        self.enqueue(body='',payload={'seq':1,'incoming':'花盆好看吗？','reaction':True})
        (self.root/'sticker.png').write_bytes(b'mocked picture')
        async def upload(*args):
            self.store.set_preference('101','称呼','新称呼')
            return object()
        self.svc.upload_picture.side_effect=upload
        with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
            await self.svc.send_one();send.assert_not_awaited()
        self.assertEqual(self.store.job('job')['body'],'')

    async def test_scheduled_pause_during_card_preparation_prevents_upload(self):
        self.store.set_birthday('101',1,1,'solar','explicit')
        self.enqueue(kind='birthday_card')
        async def prepare(job):
            self.store.set('scheduled_paused',True)
            return self.root/'card.jpg'
        self.svc.prepare_birthday_card=prepare
        with patch('cloud_social.service.user.User') as user:
            user.return_value.get_relation=AsyncMock(return_value={
                'relation':{'attribute':2},'be_relation':{'attribute':4}})
            await self.svc.send_one()
        self.svc.upload_picture.assert_not_awaited()

    async def test_global_poll_freshness_does_not_substitute_for_recipient(self):
        now=time.time();self.enqueue()
        self.store.set('authentication_verified_at',now-2);self.store.set('last_poll_ok',now)
        with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
            await self.svc.send_one();send.assert_not_awaited()
            self.store.set('last_poll_uid:101',time.time());self.store.update_job('job',available=0)
            await self.svc.send_one();send.assert_awaited_once()

    async def test_pending_recipient_is_polled_outside_recent_session_list(self):
        job=self.enqueue(kind='morning')
        fetch=await self.poll(self.message(1,'拒收通知'),rows=[])
        fetch.assert_awaited_once_with(101,self.svc.credential,begin_seqno=0)
        self.assertTrue(self.store.user('101')['flags']['no_proactive'])
        self.assertGreater(self.store.get('last_poll_uid:101'),0)
        self.assertFalse(self.svc.ready_for_dispatch(job,time.time()))
        self.assertEqual(self.store.job('job')['status'],'blocked')

    async def test_unknown_platform_events_and_other_senders_do_not_create_jobs(self):
        await self.poll(self.message(1,'无需回复的事件',kind=18),
                        self.message(2,'系统事件',kind=7),self.message(3,'其他会话',uid='102'))
        self.assertEqual(self.store.stats(),{})
        self.assertEqual(self.store.latest_inbound_seq('101'),0)

    async def test_known_system_session_is_never_fetched(self):
        fetch=await self.poll(rows=[{'talker_id':'844424930131966'}])
        fetch.assert_not_awaited()

    async def test_short_text_turn_is_generated_once_and_retains_all_receipts(self):
        await self.poll(*(self.message(i,f'问题{i}',age=(6-i)*5) for i in range(1,7)))
        with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
            await self.svc.send_one();self.store.set('outbound_after',0)
            await self.svc.send_one();send.assert_awaited_once()
        self.svc.ai.reply.assert_awaited_once()
        history=self.svc.ai.reply.await_args.args[1]
        self.assertEqual([m['content'] for m in history],[f'问题{i}' for i in range(1,7)])
        self.assertEqual(self.store.stats(),{'sent':6})
        self.store.recover()
        self.assertIsNone(self.store.next_job(time.time()))

    async def test_command_and_image_are_merge_barriers(self):
        await self.poll(self.message(1,'第一个问题'),self.message(2,'拒收早安'),
                        self.message(3,'第二个问题'),self.message(4,'',kind=2))
        first=self.store.coalesce_dm(self.store.job('dm:101:1'),time.time())
        self.assertNotIn('merged_ids',json.loads(first['payload']))
        self.assertTrue(self.store.job('dm:101:2')['body'])
        self.assertTrue(json.loads(self.store.job('dm:101:4')['payload'])['reaction'])
        self.assertTrue(self.store.user('101')['flags']['no_morning'])

    async def test_new_message_during_generation_prevents_stale_draft(self):
        await self.poll(self.message(1,'第一个问题'))
        async def reply(*args,**kwargs):
            await self.poll(self.message(1,'第一个问题'),self.message(2,'第二个问题'))
            return '已经过时的草稿。','mock'
        self.svc.ai.reply.side_effect=reply
        with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
            await self.svc.send_one();send.assert_not_awaited()
        self.assertEqual(self.store.job('dm:101:1')['body'],'')
        self.svc.ai.reply.side_effect=None
        with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
            await self.svc.send_one();send.assert_awaited_once()
        self.assertEqual(self.store.stats(),{'sent':2})

    async def test_user_request_window_defers_generation_without_losing_job(self):
        self.svc.cfg.update(user_request_limit=1,user_request_window_seconds=600)
        self.store.claim_user_request('101',time.time(),limit=1)
        self.enqueue(body='',payload={'seq':1,'incoming':'第一个问题'})
        self.enqueue(key='other',uid='102',body='',payload={'seq':1,'incoming':'另一个问题'})
        with patch('cloud_social.service.session.send_msg',AsyncMock()) as send:
            await self.svc.send_one();send.assert_not_awaited()
            self.assertEqual(self.store.job('job')['status'],'queued')
            self.assertGreater(self.store.job('job')['available'],time.time())
            await self.svc.send_one();send.assert_awaited_once()
        self.svc.ai.reply.assert_awaited_once()
        self.assertEqual(self.svc.ai.reply.await_args.args[0],'102')

    async def test_merged_timeout_is_terminal_for_every_source_message(self):
        await self.poll(self.message(1,'第一个问题'),self.message(2,'第二个问题'))
        with patch('cloud_social.service.session.send_msg',AsyncMock(side_effect=TimeoutError)) as send:
            await self.svc.send_one();self.store.set('outbound_after',0)
            self.store.recover();await self.svc.send_one();send.assert_awaited_once()
        self.assertEqual(self.store.stats(),{'unknown':2})

    async def test_crashed_merged_dispatch_recovers_all_receipts_as_unknown(self):
        await self.poll(self.message(1,'第一个问题'),self.message(2,'第二个问题'))
        self.store.coalesce_dm(self.store.job('dm:101:1'),time.time())
        self.store.update_job('dm:101:1',status='dispatching')
        self.store.recover()
        self.assertEqual(self.store.stats(),{'unknown':2})
        self.assertIsNone(self.store.next_job(time.time()))

    def test_queue_rotates_users_and_old_low_priority_jobs_are_not_starved(self):
        self.enqueue(key='a1');self.enqueue(key='a2');self.enqueue(key='b',uid='102')
        self.assertEqual(self.store.next_job(time.time())['uid'],'101')
        self.assertEqual(self.store.next_job(time.time())['uid'],'102')
        self.enqueue(key='old',uid='103',kind='morning',priority=9)
        self.store.db.execute("UPDATE jobs SET created=? WHERE id='old'",(time.time()-301,));self.store.db.commit()
        self.assertEqual(self.store.next_job(time.time())['id'],'old')

    def test_encrypted_backup_stops_new_plaintext_snapshots_and_default_keeps_bodies(self):
        self.svc.cfg['encrypted_backup']={'enabled':True}
        self.enqueue()
        self.svc.maintain_private_data()
        self.assertFalse((self.root/'backups').exists())
        self.assertTrue(self.store.job('job')['body'])

    def test_optional_body_retention_preserves_consent_and_unknown_receipts(self):
        self.svc.cfg.update(encrypted_backup={'enabled':True},message_body_retention_days=1)
        self.store.update_user('101',no_proactive=True)
        self.enqueue(payload={'seq':1,'incoming':'临时正文','parent':'original'})
        self.store.update_job('job',status='unknown')
        self.store.db.execute("UPDATE jobs SET created=?",(time.time()-2*86400,));self.store.db.commit()
        self.svc.maintain_private_data()
        job=self.store.job('job')
        self.assertEqual(job['status'],'unknown');self.assertEqual(job['body'],'')
        self.assertEqual(json.loads(job['payload']),{'seq':1,'parent':'original'})
        self.assertTrue(self.store.user('101')['flags']['no_proactive'])


if __name__=='__main__':unittest.main()
