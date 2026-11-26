"""Durable queue and receipts. Never automatically resend an ambiguous dispatch."""
import json
from contextlib import closing
import sqlite3
import time
from pathlib import Path

TERMINAL=('sent','blocked','unknown','failed','expired','coalesced')


def _finish_coalesced_jobs(db, parents, status, sent_at=None):
    for (payload,) in parents:
        children = json.loads(payload).get('merged_ids', [])
        if children:
            db.executemany("UPDATE jobs SET status=?,sent_at=? WHERE id=? AND status='coalesced'",
                           ((status, sent_at, child) for child in children))


def mark_interrupted_dispatches_unknown(db):
    """Mark ambiguous parents and their merged receipts in the caller's transaction."""
    parents = db.execute("UPDATE jobs SET status='unknown' WHERE status='dispatching' RETURNING payload").fetchall()
    _finish_coalesced_jobs(db, parents, 'unknown')


class Store:
    def __init__(self,path,recover=False):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(uid TEXT PRIMARY KEY,name TEXT NOT NULL,flags TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,uid TEXT NOT NULL,kind TEXT NOT NULL,day TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',payload TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL,available REAL NOT NULL,expires REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,sent_at REAL,provider TEXT,created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,available,priority,created);
            CREATE INDEX IF NOT EXISTS jobs_campaign ON jobs(kind,day,status);
            CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY,kind TEXT NOT NULL,day TEXT NOT NULL,done_at REAL);
            CREATE TABLE IF NOT EXISTS messages(uid TEXT NOT NULL,id TEXT NOT NULL,seq INTEGER NOT NULL,
                sender TEXT NOT NULL,text TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(uid,id));
            CREATE TABLE IF NOT EXISTS preferences(uid TEXT NOT NULL,name TEXT NOT NULL,value TEXT NOT NULL,
                updated REAL NOT NULL,PRIMARY KEY(uid,name));
            CREATE TABLE IF NOT EXISTS birthdays(uid TEXT PRIMARY KEY,month INTEGER NOT NULL,day INTEGER NOT NULL,
                calendar TEXT NOT NULL,source TEXT NOT NULL,updated REAL NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
        ''')
        if recover:self.recover()

    def recover(self):
        # Call only after obtaining the exclusive service lock.
        with self.db:
            mark_interrupted_dispatches_unknown(self.db)
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='generating'")

    def get(self,key,default=None):
        row=self.db.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self,key,value):
        with self.db:self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))

    def user(self,uid):
        row=self.db.execute('SELECT * FROM users WHERE uid=?',(str(uid),)).fetchone()
        return {'uid':str(uid),'name':row['name'] if row else '', 'flags':json.loads(row['flags']) if row else {}}

    def update_user(self,uid,name=None,**flags):
        old=self.user(uid);old['flags'].update(flags)
        with self.db:self.db.execute('INSERT OR REPLACE INTO users VALUES (?,?,?)',
            (str(uid),str(old['name'] if name is None else name),json.dumps(old['flags'],ensure_ascii=False)))

    def enqueue(self,key,uid,kind,day,payload=None,body='',priority=5,available=None,expires=None):
        with self.db:
            return self._enqueue(key,uid,kind,day,payload,body,priority,available,expires)

    def _enqueue(self,key,uid,kind,day,payload=None,body='',priority=5,available=None,expires=None):
        now=time.time()
        return bool(self.db.execute('''INSERT OR IGNORE INTO jobs
                (id,uid,kind,day,payload,body,priority,available,expires,created)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (key,str(uid),kind,day,json.dumps(payload or {},ensure_ascii=False),body,priority,
                 now if available is None else available,now+86400 if expires is None else expires,now)).rowcount)

    def job(self,key):
        row=self.db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
        return dict(row) if row else None

    def update_job(self,key,**values):
        allowed={'status','body','available','attempts','sent_at','provider','payload'}
        assert set(values)<=allowed
        with self.db:
            self.db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',list(values.values())+[key])
            if values.get('status') in TERMINAL and values['status']!='coalesced':
                parents = self.db.execute('SELECT payload FROM jobs WHERE id=?', (key,)).fetchall()
                _finish_coalesced_jobs(self.db, parents, values['status'], values.get('sent_at'))

    def next_job(self,now):
        with self.db:
            expired = self.db.execute("""UPDATE jobs SET status='expired' WHERE status='queued' AND expires<=?
                RETURNING payload""", (now,)).fetchall()
            _finish_coalesced_jobs(self.db, expired, 'expired')
        # Announcements yield to ordinary jobs, which still get the age bonus.
        last=self.get('queue_last_uid','')
        row=self.db.execute("""SELECT * FROM jobs WHERE status='queued' AND available<=? AND expires>?
            ORDER BY CASE WHEN kind='announcement' THEN 1 ELSE 0 END,
              CASE WHEN created<=? THEN 0 ELSE 1 END,
              CASE WHEN created<=? THEN created ELSE 0 END,
              CASE WHEN uid=? THEN 1 ELSE 0 END,priority,created,id LIMIT 1""",
            (now,now,now-300,now-300,last)).fetchone()
        if row:self.set('queue_last_uid',row['uid'])
        return dict(row) if row else None

    def pending_recipients(self,now,limit=4):
        return [r[0] for r in self.db.execute("""SELECT uid FROM jobs
            WHERE status='queued' AND expires>? GROUP BY uid
            ORDER BY MIN(CASE WHEN kind='announcement' THEN 1 ELSE 0 END),MIN(created),MIN(priority) LIMIT ?""",
            (now,max(1,int(limit))))]

    def coalesce_dm(self,job,now,window=45,max_items=6,max_chars=1600):
        """Keep each inbound key durable while making one draft for a short text turn."""
        payload=json.loads(job['payload'])
        if (job['kind']!='dm' or job['status']!='queued' or job['body'] or job['attempts']
                or not payload.get('mergeable') or payload.get('image_url')):return job
        parts=list(payload.get('merged_texts') or [payload.get('incoming','')])
        merged=list(payload.get('merged_ids') or [])
        rows=self.db.execute("""SELECT * FROM jobs WHERE uid=? AND kind='dm' AND status='queued'
            AND id!=? AND created>=? ORDER BY created,id""",(job['uid'],job['id'],job['created'])).fetchall()
        with self.db:
            for row in rows:
                item=json.loads(row['payload'])
                if (row['created']>job['created']+max(0,window) or row['expires']<=now
                        or abs(float(item.get('received',row['created']))-float(payload.get('received',job['created'])))>max(0,window)
                        or row['body'] or row['attempts'] or not item.get('mergeable')
                        or item.get('image_url') or item.get('merged_ids')):break
                text=item.get('incoming','')
                if len(parts)>=max_items or sum(map(len,parts))+len(text)>max_chars:break
                parts.append(text);merged.append(row['id'])
                payload['seq']=max(payload.get('seq',0),item.get('seq',0))
                self.db.execute("UPDATE jobs SET status='coalesced' WHERE id=? AND status='queued'",(row['id'],))
            if merged:
                payload.update(incoming='\n'.join(parts),merged_ids=merged,merged_texts=parts)
                self.db.execute("UPDATE jobs SET payload=? WHERE id=?",(json.dumps(payload,ensure_ascii=False),job['id']))
        return self.job(job['id'])

    def claim_user_request(self,uid,now,limit=10,period=600):
        """Reserve a paid generation attempt; retry/fallback never clears this budget."""
        limit=max(1,int(limit));period=max(10,float(period));key='request_window:'+str(uid)
        with self.db:
            recent=[float(t) for t in self.get(key,[]) if now-period<float(t)<=now]
            if len(recent)>=limit:return False,min(recent)+period
            recent.append(now)
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',(key,json.dumps(recent)))
        return True,now

    def latest_inbound_seq(self,uid):
        row=self.db.execute('SELECT MAX(seq) FROM messages WHERE uid=? AND sender=?',(str(uid),str(uid))).fetchone()
        return int(row[0] or 0)

    def expire_message_bodies(self,before):
        """Optional retention: preserve consent, job identifiers and delivery receipts."""
        with self.db:
            self.db.execute("UPDATE messages SET text='' WHERE at<?",(before,))
            rows = self.db.execute("""SELECT id,payload FROM jobs
                WHERE created<? AND status IN ('sent','blocked','unknown','failed','expired','coalesced')
                AND (status!='coalesced' OR id NOT IN (
                    SELECT child.value FROM jobs AS parent JOIN json_each(parent.payload,'$.merged_ids') AS child
                    WHERE parent.status IN ('queued','generating','dispatching') AND child.type='text'
                ))""", (before,)).fetchall()
            for row in rows:
                payload=json.loads(row['payload'])
                for name in ('incoming','merged_texts','image_url'):payload.pop(name,None)
                self.db.execute("UPDATE jobs SET body='',payload=? WHERE id=?",(json.dumps(payload,ensure_ascii=False),row['id']))

    def defer_dm_expiry(self,now,pause_until):
        # Keep recent replies through cooldown with 30 minutes to drain the queue.
        with self.db:
            self.db.execute("""UPDATE jobs SET expires=MAX(expires,MIN(created+21600,?))
                WHERE kind='dm' AND status IN ('queued','generating') AND expires>?""",
                (pause_until+1800,now))

    def campaign(self,kind,day):
        row=self.db.execute('SELECT * FROM campaigns WHERE id=?',(kind+':'+day,)).fetchone()
        return dict(row) if row else None

    def create_campaign(self,kind,day,entries):
        with self.db:
            if not self.db.execute('INSERT OR IGNORE INTO campaigns VALUES (?,?,?,NULL)',(kind+':'+day,kind,day)).rowcount:return False
            for entry in entries:self._enqueue(**entry)
        return True

    def finish_campaign(self,kind,day,now):
        pending=self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind=? AND day=? AND status IN ('queued','generating','dispatching')",(kind,day)).fetchone()[0]
        if not pending:
            with self.db:self.db.execute('UPDATE campaigns SET done_at=COALESCE(done_at,?) WHERE id=?',(now,kind+':'+day))

    def sent_users(self,kind,day):
        return [r[0] for r in self.db.execute("SELECT uid FROM jobs WHERE kind=? AND day=? AND status='sent'",(kind,day))]

    def add_message(self,uid,message):
        key=str(message.get('msg_key') or message.get('msg_seqno'))
        raw=message.get('content') or '{}'
        try:text=json.loads(raw).get('content','') if isinstance(raw,str) else raw.get('content','')
        except (ValueError,AttributeError):text=''
        with self.db:self.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)',
            (str(uid),key,int(message.get('msg_seqno') or 0),str(message.get('sender_uid') or ''),str(text)[:2000],float(message.get('timestamp') or 0)))
        return str(text)

    def context(self,uid,seq,me):
        rows=list(self.db.execute('SELECT sender,text FROM messages WHERE uid=? AND seq<=? AND text!=\'\' ORDER BY seq DESC LIMIT 12',(str(uid),seq)))
        return [{'role':'assistant' if r['sender']==str(me) else 'user','content':r['text']} for r in reversed(rows)]

    def stats(self):
        return {r[0]:r[1] for r in self.db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status')}

    def backup(self,target):
        target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(target)) as dest:self.db.backup(dest)

    def preferences(self,uid):
        return {r['name']:r['value'] for r in self.db.execute(
            'SELECT name,value FROM preferences WHERE uid=? ORDER BY name',(str(uid),))}

    def set_preference(self,uid,name,value):
        with self.db:self.db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?,?,?)',
            (str(uid),name,value,time.time()))
        with self.db:self.db.execute("UPDATE jobs SET body='',provider=NULL,status='queued' WHERE uid=? AND kind='dm' "
            "AND status IN ('queued','generating') AND provider IS NOT NULL",(str(uid),))
        self.set('preference_revision:'+str(uid),self.get('preference_revision:'+str(uid),0)+1)

    def delete_preference(self,uid,name=None):
        with self.db:
            if name is None:self.db.execute('DELETE FROM preferences WHERE uid=?',(str(uid),))
            else:self.db.execute('DELETE FROM preferences WHERE uid=? AND name=?',(str(uid),name))
            # Do not send a draft generated from preferences that have just been removed.
            self.db.execute("UPDATE jobs SET body='',provider=NULL,status='queued' WHERE uid=? AND kind='dm' "
                "AND status IN ('queued','generating') AND provider IS NOT NULL",(str(uid),))
        self.set('preference_revision:'+str(uid),self.get('preference_revision:'+str(uid),0)+1)

    def set_birthday(self,uid,month,day,calendar,source):
        with self.db:self.db.execute('INSERT OR REPLACE INTO birthdays VALUES (?,?,?,?,?,?,1)',
            (str(uid),month,day,calendar,source,time.time()))

    def cancel_birthday(self,uid):
        with self.db:
            self.db.execute('UPDATE birthdays SET enabled=0 WHERE uid=?',(str(uid),))
            self.db.execute("UPDATE jobs SET status='blocked' WHERE uid=? AND kind IN ('birthday','birthday_card') "
                "AND status IN ('queued','generating')",(str(uid),))

    def import_seed(self,seed):
        if self.get('seed_imported'):return
        for row in seed.get('users',[]):self.update_user(row['uid'],row.get('name',''),**row.get('flags',{}))
        for receipt in seed.get('receipts',[]):
            self.enqueue(receipt['id'],receipt['uid'],receipt['kind'],receipt['day'])
            self.update_job(receipt['id'],status=receipt['status'])
        self.set('pause_until',seed.get('pause_until',0))
        self.set('seed_imported',True)
