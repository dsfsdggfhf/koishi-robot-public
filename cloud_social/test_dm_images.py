import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, patch

from bilibili_api import session

from .dm_images import message_url, safe_url
from .service import IMAGE_FALLBACK, Service
from .store import Store


IMAGE_URL = 'https://message.biliimg.com/bfs/im/test.jpg'


class DMImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root/'sticker.png').write_bytes(b'mock sticker')
        self.svc = Service.__new__(Service)
        self.store = self.svc.store = Store(root/'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.svc.cfg = {'listen_since': time.time()-600, 'stickers_dir': str(root),
                        'vision_provider': {'id': 'rightapi_vision'}}
        self.svc.me = '999'
        self.svc.credential = object()
        self.svc.log = lambda *args, **kwargs: None
        self.svc.ai = type('AI', (), {'reply': AsyncMock(return_value=('我看见了蓝色的小帽子。', 'rightapi_vision'))})()
        self.svc.upload_picture = AsyncMock(return_value=object())

    def message(self, seq, kind, data, *, age=0, uid='101'):
        return {'msg_seqno': seq, 'msg_key': str(seq), 'sender_uid': uid,
                'msg_type': kind, 'timestamp': time.time()-age, 'content': json.dumps(data)}

    async def poll(self, *messages):
        async def read(fn):
            return await fn()
        self.svc.read_api = read
        with patch('cloud_social.service.session.get_sessions', AsyncMock(return_value={'session_list': [{'talker_id': 101}]})), \
             patch('cloud_social.service.session.fetch_session_msgs', AsyncMock(return_value={'messages': messages})):
            await self.svc.poll()

    async def test_bare_image_uses_local_sticker_and_deduplicates(self):
        msg = self.message(1, 2, {'url': IMAGE_URL})
        await self.poll(msg)
        await self.poll(msg)
        with patch('cloud_social.service.session.send_msg', AsyncMock()) as send:
            await self.svc.send_one()
            send.assert_awaited_once_with(self.svc.credential, 101, session.EventType.PICTURE,
                                          self.svc.upload_picture.return_value)
        self.svc.ai.reply.assert_not_awaited()
        self.assertEqual(self.store.stats(), {'sent': 1})

    async def test_excluded_sticker_uses_text_without_model(self):
        self.store.update_user('101', no_stickers=True)
        await self.poll(self.message(1, 2, {'url': IMAGE_URL}))
        with patch('cloud_social.service.session.send_msg', AsyncMock()) as send:
            await self.svc.send_one()
            send.assert_awaited_once_with(self.svc.credential, 101, session.EventType.TEXT, '收到图啦～')
        self.svc.upload_picture.assert_not_awaited()
        self.svc.ai.reply.assert_not_awaited()

    async def test_question_routes_one_image_to_vision(self):
        await self.poll(self.message(1, 2, {'url': IMAGE_URL}), self.message(2, 1, {'content': '这是谁？'}))
        self.store.update_job('dm:101:1', status='sent')
        with patch('cloud_social.service.image_data_url', AsyncMock(return_value='data:image/jpeg;base64,AA==')) as fetch, \
             patch('cloud_social.service.session.send_msg', AsyncMock()) as send:
            await self.svc.send_one()
            fetch.assert_awaited_once_with(IMAGE_URL)
            self.svc.ai.reply.assert_awaited_once()
            call = self.svc.ai.reply.await_args
            self.assertEqual(call.kwargs, {'vision': True})
            self.assertEqual(call.args[1][-1]['content'][0]['text'], '这是谁？')
            self.assertEqual(call.args[1][-1]['content'][1]['image_url']['url'], 'data:image/jpeg;base64,AA==')
            send.assert_awaited_once()

    async def test_old_or_other_sender_image_cannot_be_forwarded(self):
        await self.poll(self.message(1, 2, {'url': IMAGE_URL}, age=190),
                        self.message(2, 2, {'url': IMAGE_URL}, uid='102'),
                        self.message(3, 1, {'content': '这是谁？'}))
        self.assertNotIn('image_url', json.loads(self.store.job('dm:101:3')['payload']))

    async def test_cutover_does_not_reply_to_unprocessed_old_image(self):
        self.svc.cfg['dm_images_since'] = time.time()
        await self.poll(self.message(1, 2, {'url': IMAGE_URL}, age=30))
        self.assertIsNone(self.store.job('dm:101:1'))

    async def test_vision_timeout_sends_fallback_without_retry(self):
        await self.poll(self.message(1, 2, {'url': IMAGE_URL, 'content': '图上写了什么？'}))
        self.svc.ai.reply.side_effect = TimeoutError
        with patch('cloud_social.service.image_data_url', AsyncMock(return_value='data:image/jpeg;base64,AA==')), \
             patch('cloud_social.service.session.send_msg', AsyncMock()) as send:
            await self.svc.send_one()
            self.svc.ai.reply.assert_awaited_once()
            send.assert_awaited_once_with(self.svc.credential, 101, session.EventType.TEXT, IMAGE_FALLBACK)
        self.assertEqual(self.store.job('dm:101:1')['attempts'], 1)

    async def test_restart_after_claim_cannot_repeat_vision(self):
        await self.poll(self.message(1, 2, {'url': IMAGE_URL, 'content': '图上写了什么？'}))
        self.store.update_job('dm:101:1', status='generating', attempts=1)
        self.store.recover()
        with patch('cloud_social.service.session.send_msg', AsyncMock()) as send:
            await self.svc.send_one()
            send.assert_awaited_once_with(self.svc.credential, 101, session.EventType.TEXT, IMAGE_FALLBACK)
        self.svc.ai.reply.assert_not_awaited()

    def test_image_urls_are_restricted_to_bilibili_cdn(self):
        self.assertEqual(safe_url('//message.biliimg.com/bfs/im/test.jpg'), IMAGE_URL)
        for url in ('https://message.biliimg.com.attacker.invalid/x', 'https://127.0.0.1/x',
                    'https://u@i0.hdslb.com/x', 'https://i0.hdslb.com:8080/x'):
            self.assertEqual(message_url(self.message(1, 2, {'url': url})), '')
