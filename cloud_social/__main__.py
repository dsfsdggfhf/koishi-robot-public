import argparse
import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('action',choices=['run','check','status','pause','resume',
                                         'pause-outbound','resume-outbound','release-restore-hold','announcement'])
    parser.add_argument('--confirm-reconciled',action='store_true')
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--apply',metavar='SHA256')
    parser.add_argument('--expected-mutual',type=int,metavar='COUNT')
    args=parser.parse_args()
    if args.action!='announcement' and any(value is not None for value in (args.manifest,args.apply,args.expected_mutual)):
        parser.error('--manifest, --apply and --expected-mutual are only for announcement')
    config_path=Path(args.config).resolve()
    if args.action=='status':
        path=config_path.parent/'state.sqlite3'
        if not path.exists():print(json.dumps({'running_state_exists':False}));return
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            stats={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status')}
            values={r[0]:json.loads(r[1]) for r in db.execute("SELECT key,value FROM kv WHERE key IN ('heartbeat','pause_until','scheduled_paused','emergency_send_hold','restore_send_hold','last_poll_ok','last_poll_error','platform_last_error','risk_state','authentication_verified_at') OR key LIKE 'ai_count:%' OR key LIKE 'last_ai_error:%'")}
        print(json.dumps({'jobs':stats,'state':values},ensure_ascii=False,indent=2));return
    from .service import Service
    service=Service(config_path)
    if args.action=='announcement':
        if args.manifest is None:parser.error('announcement requires --manifest')
        if (args.apply is None)!=(args.expected_mutual is None):
            parser.error('--apply requires --expected-mutual and vice versa')
        from .announcement import AnnouncementError,prepare
        for name in ('httpx','httpcore','bilibili_api'):logging.getLogger(name).setLevel(logging.ERROR)
        try:summary=asyncio.run(prepare(service,args.manifest,args.apply,args.expected_mutual))
        except AnnouncementError as exc:raise SystemExit(str(exc)) from None
        except Exception as exc:raise SystemExit('announcement_preparation_failed: '+type(exc).__name__) from None
        print(json.dumps(summary,ensure_ascii=False));return
    if args.action in ('pause','resume'):
        service.store.set('scheduled_paused',args.action=='pause')
        print('scheduled greeting pause updated');return
    if args.action in ('pause-outbound','resume-outbound'):
        service.store.set('emergency_send_hold',args.action=='pause-outbound')
        print('account outbound hold updated');return
    if args.action=='release-restore-hold':
        if not args.confirm_reconciled:parser.error('confirm one sender and reconcile recent opt-outs and receipts first')
        if service.store.get('restore_send_hold',False):
            service.store.set('emergency_send_hold',True)
            service.store.set('restore_send_hold',False)
        print('restore hold released; emergency send hold remains until explicitly resumed');return
    if args.action=='check':
        for name,items in service.pools.items():
            if not items:raise ValueError('empty '+name+' pool')
        assert service.cfg['listen_since']>0
        assert len(service.cfg['providers'])==2
        assert all(service.secrets.get(p['key_name']) for p in service.cfg['providers'])
        print(json.dumps({'configuration':'ok','morning_templates':len(service.pools['morning']),
            'night_templates':len(service.pools['nightly']),'solar_terms':len(service.calendar.terms),
            'holiday_delay_seconds':180,'scheduled_start_date':service.cfg['scheduled_start_date'],
            'provider_names':[p['id'] for p in service.cfg['providers']]},ensure_ascii=False));return
    import fcntl
    lock=(config_path.parent/'service.lock').open('w')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('another cloud instance already owns this account')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    for name in ('httpx','httpcore','bilibili_api'):logging.getLogger(name).setLevel(logging.ERROR)
    try:asyncio.run(service.run())
    except KeyboardInterrupt:pass
    except Exception as exc:
        logging.error('cloud service stopped: %s',type(exc).__name__)
        raise SystemExit(1)
    finally:lock.close()


if __name__=='__main__':main()
