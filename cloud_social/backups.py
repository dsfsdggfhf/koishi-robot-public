"""Authenticated encrypted snapshots. The decryption key stays off the server."""
import argparse
from contextlib import closing
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tarfile
import tempfile
import time

from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import padding,rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import mark_interrupted_dispatches_unknown

FILES={'state.sqlite3','config.json','secrets.json','seed.json'}
AAD=b'koishi-private-backup-v1'


def private_key_bytes(path):
    """Do not follow a replacement link or silently accept a shared POSIX key."""
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('unsafe_restore_key_file')
    if os.name!='nt' and path.stat().st_mode & 0o077:
        raise PermissionError('restore_key_requires_owner_only_permissions')
    return path.read_bytes()


def protect_new_private_key(path):
    """Restrict newly generated keys; never rewrite or migrate an existing key."""
    if os.name=='nt':
        import re
        import subprocess
        identity=subprocess.run(['whoami','/user','/fo','csv','/nh'],check=True,
            capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        # whoami uses the Windows OEM encoding; only the ASCII SID is needed.
        match=re.search(rb'S-1-\d+(?:-\d+)+',identity.stdout or b'')
        if not match:raise RuntimeError('cannot_identify_key_owner')
        sid=match[0].decode('ascii')
        subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r','*'+sid+':(F)'],
            check=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        Path(path).chmod(0o600)


def keypair(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    private=folder/'restore-private.pem';public=folder/'backup-public.pem'
    if private.exists():
        key=serialization.load_pem_private_key(private_key_bytes(private),None)
    else:
        key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        descriptor=os.open(private,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            # Apply the Windows ACL before any private bytes reach the file.
            protect_new_private_key(private)
            with os.fdopen(descriptor,'wb') as f:
                descriptor=None
                f.write(key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        except BaseException:
            if descriptor is not None:os.close(descriptor)
            private.unlink(missing_ok=True)
            raise
    expected=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    if public.exists() and public.read_bytes()!=expected:raise ValueError('backup_public_key_mismatch')
    public.write_bytes(expected)
    return private,public


def encrypt(raw,public):
    key=serialization.load_pem_public_key(Path(public).read_bytes())
    aes=secrets.token_bytes(32);nonce=secrets.token_bytes(12)
    wrapped=key.encrypt(aes,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
    return json.dumps({'format':1,'key':base64.b64encode(wrapped).decode(),'nonce':base64.b64encode(nonce).decode(),
        'data':base64.b64encode(AESGCM(aes).encrypt(nonce,raw,AAD)).decode()},separators=(',',':')).encode()


def validate_envelope(blob):
    """Check the transport object; authentication still requires decryption."""
    if len(blob)>256*1024*1024:raise ValueError('backup_object_too_large')
    envelope=json.loads(blob)
    if not isinstance(envelope,dict) or set(envelope)!={'format','key','nonce','data'} or envelope['format']!=1:
        raise ValueError('unsupported_backup_format')
    wrapped=base64.b64decode(envelope['key'],validate=True)
    nonce=base64.b64decode(envelope['nonce'],validate=True)
    data=base64.b64decode(envelope['data'],validate=True)
    if not 256<=len(wrapped)<=1024 or len(nonce)!=12 or len(data)<16:
        raise ValueError('invalid_backup_envelope')
    return envelope


def decrypt(blob,private):
    envelope=validate_envelope(blob)
    key=serialization.load_pem_private_key(private_key_bytes(private),None)
    aes=key.decrypt(base64.b64decode(envelope['key']),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
    return AESGCM(aes).decrypt(base64.b64decode(envelope['nonce']),base64.b64decode(envelope['data']),AAD)


def snapshot(store,private,public):
    private=Path(private)
    with tempfile.TemporaryDirectory(dir=private) as folder:
        db=Path(folder)/'state.sqlite3';store.backup(db)
        with closing(sqlite3.connect(db)) as c:
            if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('snapshot_integrity_failed')
        files={name:(db if name=='state.sqlite3' else private/name).read_bytes() for name in FILES}
    manifest={'format':1,'at':time.time(),'sha256':{name:hashlib.sha256(raw).hexdigest() for name,raw in files.items()}}
    files['manifest.json']=json.dumps(manifest).encode()
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tar:
        for name,raw in files.items():
            info=tarfile.TarInfo(name);info.size=len(raw);info.mode=0o600
            tar.addfile(info,io.BytesIO(raw))
    return encrypt(buf.getvalue(),public)


def unpack(blob,private):
    raw=decrypt(blob,private)
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tar:
        members=tar.getmembers()
        if len(members)!=5 or {m.name for m in members}!=FILES|{'manifest.json'}:raise ValueError('unexpected_backup_members')
        if any(not m.isfile() or m.size>256*1024*1024 for m in members):raise ValueError('invalid_backup_member')
        files={m.name:tar.extractfile(m).read() for m in members}
    manifest=json.loads(files.pop('manifest.json'))
    if set(manifest['sha256'])!=FILES or any(hashlib.sha256(raw).hexdigest()!=manifest['sha256'][name] for name,raw in files.items()):
        raise ValueError('backup_hash_mismatch')
    return files


def restore(blob,private,target):
    target=Path(target)
    if target.exists() and any(target.iterdir()):raise ValueError('restore_target_must_be_empty')
    files=unpack(blob,private);target.mkdir(parents=True,exist_ok=True);target.chmod(0o700)
    for name,raw in files.items():
        path=target/name;path.write_bytes(raw);path.chmod(0o600)
    with closing(sqlite3.connect(target/'state.sqlite3')) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('restored_database_invalid')
        # A restored snapshot is never immediately eligible to send. Release
        # requires a separate maintenance decision after latest-state merging.
        with db:
            for name,value in (('restore_send_hold',True),('scheduled_paused',True),
                               ('restore_created_at',time.time())):
                db.execute('INSERT OR REPLACE INTO kv(key,value) VALUES (?,?)',(name,json.dumps(value)))
            mark_interrupted_dispatches_unknown(db)


def export(private):
    # Authorized_keys forced-command entry. Read only encrypted backup objects;
    # no shell, arbitrary files, or caller-provided remote paths.
    folder=Path(private)/'encrypted_backups'
    request=json.loads(sys.stdin.readline(2048))
    if request.get('action')=='list':
        result=[{'name':p.name,'size':p.stat().st_size} for p in sorted(folder.glob('state_*.kbackup'))[-14:]]
        sys.stdout.buffer.write(json.dumps(result).encode())
    elif request.get('action')=='get':
        name=request.get('name','')
        if Path(name).name!=name or not name.startswith('state_') or not name.endswith('.kbackup'):raise ValueError('invalid_backup_name')
        path=folder/name
        if path.is_symlink() or path.stat().st_size>256*1024*1024:raise ValueError('backup_object_invalid')
        sys.stdout.buffer.write(path.read_bytes())
    elif request.get('action')=='ack':
        # The client acknowledges only after download, decryption, hash & SQLite checks.
        from .store import Store
        store=Store(Path(private)/'state.sqlite3')
        store.set('offsite_verified_at',time.time());store.db.close()
        sys.stdout.write('ok')
    else:raise ValueError('invalid_backup_action')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--private',required=True);p.add_argument('action',choices=['export'])
    args=p.parse_args();export(args.private)
