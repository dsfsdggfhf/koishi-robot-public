"""Regression tests for durable short-message coalescing and retention."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from .backups import restore
from .store import Store


class CoalescedJobsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.addCleanup(self.store.db.close)

    def enqueue_pair(self, prefix, expires, uid='101'):
        for seq, suffix in enumerate(('parent', 'child'), start=1):
            key = prefix + suffix
            self.store.enqueue(key, uid, 'dm', 'day', expires=expires,
                               payload={'seq': seq, 'incoming': key, 'mergeable': True})
        parent = self.store.coalesce_dm(self.store.job(prefix + 'parent'), time.time())
        self.assertEqual(json.loads(parent['payload'])['merged_ids'], [prefix + 'child'])
        self.assertEqual(self.store.job(prefix + 'child')['status'], 'coalesced')

    def test_queue_expiry_transitions_children_and_keeps_dedupe(self):
        self.enqueue_pair('', expires=time.time() + 60)
        self.assertIsNone(self.store.next_job(time.time() + 61))
        for key in ('parent', 'child'):
            self.assertEqual(self.store.job(key)['status'], 'expired')
            self.assertFalse(self.store.enqueue(key, '101', 'dm', 'day'))

    def test_failed_child_transition_rolls_back_parent_expiry(self):
        self.enqueue_pair('', expires=time.time() + 60)
        self.store.db.executescript("""CREATE TRIGGER fail_child BEFORE UPDATE OF status ON jobs
            WHEN OLD.id='child' AND NEW.status='expired' BEGIN SELECT RAISE(ABORT,'test'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.next_job(time.time() + 61)
        self.assertEqual(self.store.job('parent')['status'], 'queued')
        self.assertEqual(self.store.job('child')['status'], 'coalesced')

    def test_restore_updates_ambiguous_parent_and_child_before_service_start(self):
        self.enqueue_pair('', expires=time.time() + 60)
        self.store.update_job('parent', status='dispatching')
        self.store.enqueue('unattempted', '102', 'dm', 'day')
        self.store.update_job('unattempted', status='generating')
        snapshot = self.root / 'backup.sqlite3'
        self.store.backup(snapshot)
        target = self.root / 'restored'
        with patch('cloud_social.backups.unpack', return_value={'state.sqlite3': snapshot.read_bytes()}):
            restore(b'', self.root / 'unused-key', target)
        restored = Store(target / 'state.sqlite3')
        self.addCleanup(restored.db.close)
        self.assertTrue(restored.get('restore_send_hold'))
        self.assertTrue(restored.get('scheduled_paused'))
        for key in ('parent', 'child'):
            self.assertEqual(restored.job(key)['status'], 'unknown')
        self.assertEqual(restored.job('unattempted')['status'], 'generating')
        restored.recover()
        self.assertEqual(restored.next_job(time.time())['id'], 'unattempted')

    def test_retention_clears_only_old_terminal_and_orphaned_work(self):
        now = time.time()
        before = now - 86400
        self.enqueue_pair('active-', now + 60)
        self.enqueue_pair('old-', now + 60, uid='102')
        self.enqueue_pair('recent-', now + 60, uid='103')
        self.store.update_user('102', no_proactive=True)
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET created=? WHERE uid IN ('101','102')", (before - 1,))
            # Simulate a database whose parent expired under the old implementation.
            self.store.db.execute("UPDATE jobs SET status='expired' WHERE id='old-parent'")
            self.store.db.execute("UPDATE jobs SET status='expired' WHERE id='recent-parent'")
        active_parent = self.store.job('active-parent')
        active_child = self.store.job('active-child')
        recent_child = self.store.job('recent-child')

        self.store.expire_message_bodies(before)

        self.assertEqual(self.store.job('active-parent'), active_parent)
        self.assertEqual(self.store.job('active-child'), active_child)
        self.assertEqual(self.store.job('recent-child'), recent_child)
        old_parent = self.store.job('old-parent')
        old_child = self.store.job('old-child')
        self.assertEqual(old_parent['status'], 'expired')
        self.assertEqual(old_child['status'], 'coalesced')
        self.assertEqual(json.loads(old_parent['payload'])['merged_ids'], ['old-child'])
        self.assertEqual(json.loads(old_child['payload']), {'seq': 2, 'mergeable': True})
        self.assertTrue(self.store.user('102')['flags']['no_proactive'])


if __name__ == '__main__':
    unittest.main()
