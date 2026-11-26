"""Offline announcement checks with a temporary outbox and a fake platform."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from .announcement import AnnouncementError, mutual_followings, prepare
from .service import Service, TZ
from .store import Store


class AnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root/'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.service = Service.__new__(Service)
        self.service.private = self.root
        self.service.store = self.store
        self.service.cfg = {'poll_seconds': 90}
        self.service.me = '999'
        self.service.credential = object()
        self.service.log = lambda *args, **kwargs: None

        async def read(fn):
            return await fn()

        self.service.read_api = read
        self.dynamic_id = '1274268890684194822'
        self.link = 'https://b23.tv/example'
        self.path = self.root/'announcement.json'
        self.manifest = {'dynamic_id': self.dynamic_id, 'link': self.link,
                         'template': '{nickname}，公开公告。\n'+self.link+'\n回复「拒收」就好啦。',
                         'exclude_uids': ['104'], 'expires_at': (datetime.now(TZ)+timedelta(hours=6)).isoformat()}
        self.write_manifest()

    def write_manifest(self):
        self.path.write_text(json.dumps(self.manifest, ensure_ascii=False), encoding='utf-8')

    def test_older_announcement_yields_to_ordinary_delivery_and_recipient_poll(self):
        now = time.time()
        self.store.enqueue('announcement', '101', 'announcement', self.dynamic_id, priority=10)
        self.store.enqueue('reply', '102', 'dm', 'day', priority=1)
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET created=? WHERE id='announcement'", (now-600,))
        self.assertEqual(self.store.pending_recipients(now, limit=1), ['102'])
        self.assertEqual(self.store.next_job(time.time())['id'], 'reply')
        self.store.update_job('reply', status='sent')
        self.assertEqual(self.store.pending_recipients(now, limit=1), ['101'])
        self.assertEqual(self.store.next_job(time.time())['id'], 'announcement')

    async def test_preview_only_then_idempotent_enqueue_excluding_local_and_cloud_blocks(self):
        self.store.update_user('102', no_proactive=True)
        self.store.update_user('103', hard_blocked=True)
        followers = {str(uid): '恋恋的朋友' for uid in range(101,105)}
        with patch('cloud_social.announcement.mutual_followings', AsyncMock(return_value=followers)) as discover:
            preview = await prepare(self.service, self.path)
            self.assertFalse(preview['enqueued'])
            self.assertEqual((preview['mutual'], preview['eligible'], preview['excluded']), (4,1,3))
            self.assertEqual(self.store.stats(), {})
            applied = await prepare(self.service, self.path, preview['sha256'], preview['mutual'])
            self.assertTrue(applied['enqueued'])
            self.assertEqual(applied['queued'], 1)
            repeated = await prepare(self.service, self.path, preview['sha256'], preview['mutual'])
            self.assertTrue(repeated['already_exists'])
            self.assertEqual(discover.await_count, 2)
        job = self.store.job(f'announcement:{self.dynamic_id}:101')
        self.assertEqual(self.store.stats(), {'queued': 1})
        self.assertIn(f'恋恋的朋友，公开公告。\n{self.link}', job['body'])

    async def test_manifest_change_and_extra_link_never_discover_or_enqueue(self):
        with patch('cloud_social.announcement.mutual_followings', AsyncMock(return_value={})) as discover:
            with self.assertRaisesRegex(AnnouncementError, 'manifest_changed'):
                await prepare(self.service, self.path, '0'*64)
            self.manifest['template'] += '\nhttps://example.org/private'
            self.write_manifest()
            with self.assertRaisesRegex(AnnouncementError, 'invalid_announcement_manifest'):
                await prepare(self.service, self.path)
            discover.assert_not_awaited()
        self.assertEqual(self.store.stats(), {})

    async def test_a_changed_mutual_count_prevents_the_commit(self):
        with patch('cloud_social.announcement.mutual_followings', AsyncMock(return_value={'101':'朋友'})):
            preview = await prepare(self.service, self.path)
            with self.assertRaisesRegex(AnnouncementError, 'mutual_count_changed'):
                await prepare(self.service, self.path, preview['sha256'], expected_mutual=2)
        self.assertEqual(self.store.stats(), {})

    async def test_exact_approved_short_link_can_send_but_regular_dm_remains_filtered(self):
        self.service.cfg['forbidden_words'] = ['https://', 'b23.tv', '禁用测试词']
        with patch('cloud_social.announcement.mutual_followings', AsyncMock(return_value={'101':'朋友'})):
            preview = await prepare(self.service, self.path)
            await prepare(self.service, self.path, preview['sha256'], preview['mutual'])
        self.store.enqueue('chat', '102', 'dm', 'day', body=f'看这个\n{self.link}')
        with patch('cloud_social.service.user.User') as profile, \
             patch('cloud_social.service.session.send_msg', AsyncMock()) as send:
            profile.return_value.get_relation = AsyncMock(return_value={
                'relation': {'attribute': 2}, 'be_relation': {'attribute': 4}})
            await self.service.send_one()
            send.assert_not_awaited()
            self.assertEqual(self.store.job('chat')['status'], 'blocked')
            await self.service.send_one()
            send.assert_awaited_once()

    async def test_approved_link_does_not_exempt_other_forbidden_text(self):
        self.service.cfg['forbidden_words'] = ['https://', 'b23.tv', '公开公告']
        with patch('cloud_social.announcement.mutual_followings', AsyncMock()) as discover:
            with self.assertRaisesRegex(AnnouncementError, 'announcement_body_rejected'):
                await prepare(self.service, self.path)
            discover.assert_not_awaited()

    async def test_mutual_discovery_checks_full_following_count_and_attributes(self):
        first = [{'mid': uid, 'uname': '朋友', 'attribute': 6 if uid==101 else 2} for uid in range(101,151)]
        last = [{'mid': 151, 'uname': '最后一页的互关', 'attribute': 6}]
        with patch('cloud_social.announcement.user.get_self_info', AsyncMock(return_value={'mid':999})), \
             patch('cloud_social.announcement.user.User') as profile:
            pages = [{'total':51, 'list':first}, {'total':51, 'list':last}]
            profile.return_value.get_followings = AsyncMock(side_effect=pages)
            self.assertEqual(await mutual_followings(self.service), {'101':'朋友', '151':'最后一页的互关'})
            self.assertEqual([c.kwargs['pn'] for c in profile.return_value.get_followings.await_args_list], [1,2])
            for last_page in ({'total':51, 'list':[]}, {'total':50, 'list':[]}, {'total':51, 'list':[first[0]]}):
                with self.subTest(last_page=last_page):
                    profile.return_value.get_followings = AsyncMock(side_effect=[pages[0],last_page])
                    with self.assertRaises(AnnouncementError):
                        await mutual_followings(self.service)

    async def test_dispatch_requires_fresh_inbox_and_live_mutual_then_honors_latest_optout(self):
        body = '你好，这是一条公告。'
        now = time.time()
        self.store.set('authentication_verified_at', now-1)
        self.store.set('last_poll_ok', now)
        self.store.enqueue('announcement', '101', 'announcement', self.dynamic_id, body=body)
        with patch('cloud_social.service.user.User') as profile, \
             patch('cloud_social.service.session.send_msg', AsyncMock()) as send:
            async def relation():
                self.store.update_user('101', no_proactive=True)
                return {'relation': {'attribute': 2}, 'be_relation': {'attribute': 4}}

            profile.return_value.get_relation = AsyncMock(side_effect=relation)
            await self.service.send_one()
            profile.return_value.get_relation.assert_not_awaited()
            self.store.set('last_poll_uid:101', now)
            self.store.update_job('announcement', available=0)
            await self.service.send_one()
            profile.return_value.get_relation.assert_awaited_once()
            send.assert_not_awaited()
        self.assertEqual(self.store.job('announcement')['status'], 'blocked')

    async def test_dispatch_honors_pause_and_blocks_unfollow(self):
        self.store.enqueue('announcement', '101', 'announcement', self.dynamic_id, body='这是一条公告。')
        with patch('cloud_social.service.user.User') as profile, \
             patch('cloud_social.service.session.send_msg', AsyncMock()) as send:
            profile.return_value.get_relation = AsyncMock(return_value={
                'relation': {'attribute': 2}, 'be_relation': {'attribute': 0}})
            self.store.set('scheduled_paused', True)
            await self.service.send_one()
            profile.return_value.get_relation.assert_not_awaited()
            self.store.set('scheduled_paused', False)
            self.store.update_job('announcement', available=0)
            await self.service.send_one()
            profile.return_value.get_relation.assert_awaited_once()
            send.assert_not_awaited()
        self.assertEqual(self.store.job('announcement')['status'], 'blocked')
