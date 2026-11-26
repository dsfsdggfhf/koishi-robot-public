"""Independent systemd timer: backups and deduplicated SMTP fault alerts."""
from datetime import datetime
from email.message import EmailMessage
import json
import logging
from pathlib import Path
import shutil
import smtplib
import ssl
import socket
import time
from urllib.parse import urlsplit

from .backups import snapshot,validate_envelope
from .store import Store

LABELS={'heartbeat':'服务心跳超过3分钟未更新','platform':'B站进入平台保护冷却',
    'poll':'私信读取超过10分钟未成功','ai':'配置的文字模型线路暂时都不可用',
    'unknown':'出现投递结果不明的消息（不会自动重发）','disk':'磁盘剩余不足512MB',
    'backup':'加密备份生成失败','offsite':'异机备份超过36小时未确认'}
LABELS['bridge']='天机电脑桥接未连接（聊天由另一站接替）'
LABELS['restore_hold']='恢复实例仍处于发送隔离，需核对最新状态和唯一发送端'


def issues(store,private,now):
    result={};heartbeat=store.get('heartbeat',0);pause=store.get('pause_until',0)
    if now-heartbeat>180:result['heartbeat']=str(int(heartbeat))
    if pause>now:result['platform']=str(int(pause))
    if pause<=now and now-store.get('last_poll_ok',now)>600:result['poll']='stale'
    cfg=json.loads((Path(private)/'config.json').read_text(encoding='utf-8-sig'))
    providers=cfg.get('providers',[])
    if cfg.get('emergency_text_provider'):
        providers=providers+[cfg['emergency_text_provider']]
    if providers and all(
        store.get('ai_pause:'+p['id'],0)>now or
        (p.get('base_url') and store.get('ai_redirect_until:'+p['base_url'].rstrip('/'),0)>now)
        for p in providers
    ):result['ai']='unavailable'
    for provider in cfg.get('providers',[]):
        url=urlsplit(provider.get('base_url',''))
        if provider['id']=='tianji' and url.hostname=='127.0.0.1':
            try:
                with socket.create_connection(('127.0.0.1',url.port or 80),timeout=2):pass
            except OSError:result['bridge']='offline'
    count=store.db.execute("SELECT COUNT(*) FROM jobs WHERE status='unknown' AND created>?",(now-86400,)).fetchone()[0]
    if count:result['unknown']=str(count)
    if shutil.disk_usage(private).free<512*1024*1024:result['disk']='low'
    since=store.get('offsite_verified_at',store.get('monitor_started',now))
    if now-since>36*3600:result['offsite']='stale'
    if store.get('restore_send_hold',False):result['restore_hold']='held'
    return result


def send_mail(settings,text):
    # Explicit server configuration only; never use a plaintext SMTP connection.
    if not settings.get('host') or not settings.get('password'):raise ValueError('smtp_not_configured')
    msg=EmailMessage();msg['Subject']='恋恋机器人异常通知'
    msg['From']=settings['from'];msg['To']=settings['to'];msg.set_content(text)
    port=int(settings.get('port',465));ctx=ssl.create_default_context()
    if port==465:
        with smtplib.SMTP_SSL(settings['host'],port,timeout=20,context=ctx) as smtp:
            smtp.login(settings.get('username',settings['from']),settings['password']);smtp.send_message(msg)
    elif port==587:
        with smtplib.SMTP(settings['host'],port,timeout=20) as smtp:
            smtp.ehlo();smtp.starttls(context=ctx);smtp.ehlo()
            smtp.login(settings.get('username',settings['from']),settings['password']);smtp.send_message(msg)
    else:raise ValueError('smtp_tls_port_required')


def tick(config_path,now=None):
    now=time.time() if now is None else now
    path=Path(config_path);private=path.parent
    cfg=json.loads(path.read_text(encoding='utf-8-sig'));store=Store(private/'state.sqlite3')
    try:
        if not store.get('monitor_started'):store.set('monitor_started',now)
        errors=issues(store,private,now)
        backup=cfg.get('encrypted_backup',{})
        if backup.get('enabled'):
            day=datetime.fromtimestamp(now).strftime('%Y-%m-%d')
            target=private/'encrypted_backups'/('state_'+day+'.kbackup')
            if not target.exists():
                try:
                    blob=snapshot(store,private,backup['public_key_file'])
                    target.parent.mkdir(mode=0o700,exist_ok=True)
                    tmp=target.with_suffix('.tmp');tmp.write_bytes(blob);tmp.chmod(0o600);tmp.replace(target)
                    store.set('encrypted_backup_at',now)
                except Exception as exc:
                    errors['backup']=type(exc).__name__
            else:
                try:
                    if target.is_symlink() or target.stat().st_size>256*1024*1024:
                        raise ValueError('invalid_backup_object')
                    validate_envelope(target.read_bytes())
                    store.set('encrypted_backup_at',target.stat().st_mtime)
                except Exception as exc:
                    errors['backup']=type(exc).__name__
            store.set('encrypted_backup_checked_at',now)
            for old in sorted(target.parent.glob('state_*.kbackup'))[:-14] if target.parent.exists() else []:old.unlink()
        state=store.get('alert_state',{})
        pending=[key for key,value in errors.items() if state.get(key,{}).get('value')!=value or now-state.get(key,{}).get('at',0)>21600]
        # An independent email channel works even when Bilibili is paused.
        settings=json.loads((private/'secrets.json').read_text(encoding='utf-8-sig')).get('smtp',{})
        store.set('alert_pending',pending)
        if pending and cfg.get('alerts',{}).get('enabled') and now-store.get('alert_attempt_at',0)>=1800:
            store.set('alert_attempt_at',now)
            text='北京时间 '+datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')+'\n'+'\n'.join('• '+LABELS[k] for k in pending)+'\n不包含私信正文、用户编号或密钥，请查看维护状态。'
            try:send_mail(settings,text)
            except Exception as exc:
                store.set('alert_last_error',{'at':now,'type':type(exc).__name__})
            else:
                state.update({k:{'at':now,'value':errors[k]} for k in pending})
                store.set('alert_state',state);store.set('alert_sent_at',now)
        store.set('monitor_checked_at',now)
        configured=bool(settings.get('host') and settings.get('password'))
        enabled=bool(cfg.get('alerts',{}).get('enabled'))
        delivery=('disabled' if not enabled else 'unconfigured' if not configured else
                  'failed' if store.get('alert_last_error',{}).get('at',0)>store.get('alert_sent_at',0)
                  else 'sent' if store.get('alert_sent_at',0) else 'not_yet_sent')
        status={'issues':sorted(errors),'pending_alerts':pending,'smtp_configured':configured,
            'alerts_enabled':enabled,'alert_delivery':delivery,'restore_send_hold':bool(store.get('restore_send_hold',False)),
            'encrypted_backup_at':store.get('encrypted_backup_at',0),'checked_at':now}
        store.set('monitor_status',status)
        return status
    finally:store.db.close()


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);args=p.parse_args()
    print(json.dumps(tick(args.config),ensure_ascii=False))
