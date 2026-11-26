"""Suppress the bundled unsigned FFmpeg console, with exact-hash rollback.

Changes only PE Subsystem (CUI=3 -> GUI=2); executable code and arguments stay
unchanged. This is for Bilidown's GUI process spawning its own ffmpeg.exe.
Updating Bilidown/FFmpeg may replace this local adjustment.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct


def digest(data):return hashlib.sha256(data).hexdigest()


def gui_image(data):
    if len(data)<256 or data[:2]!=b'MZ':raise ValueError('not_a_pe_executable')
    pe=struct.unpack_from('<I',data,0x3c)[0]
    if pe<64 or pe+24>len(data) or data[pe:pe+4]!=b'PE\0\0':raise ValueError('invalid_pe_header')
    opt=pe+24;size=struct.unpack_from('<H',data,pe+20)[0]
    if size<96 or opt+size>len(data):raise ValueError('invalid_optional_header')
    magic=struct.unpack_from('<H',data,opt)[0]
    if magic not in (0x10b,0x20b):raise ValueError('unsupported_pe_image')
    directory=opt+(96 if magic==0x10b else 112)
    number_at=opt+(92 if magic==0x10b else 108)
    if number_at+4>opt+size:raise ValueError('invalid_data_directories')
    count=struct.unpack_from('<I',data,number_at)[0]
    if count>4:
        if directory+40>opt+size:raise ValueError('invalid_certificate_directory')
        if any(struct.unpack_from('<II',data,directory+32)):raise ValueError('signed_executable_not_modified')
    offset=opt+68;subsystem=struct.unpack_from('<H',data,offset)[0]
    if subsystem not in (2,3):raise ValueError('not_a_windows_application')
    patched=bytearray(data);struct.pack_into('<H',patched,offset,2)
    return bytes(patched),offset,subsystem


def apply(exe,backup_dir):
    exe=Path(exe).resolve()
    if exe.name.lower()!='ffmpeg.exe':raise ValueError('only_explicit_ffmpeg_exe_supported')
    original=exe.read_bytes();patched,offset,subsystem=gui_image(original)
    if subsystem==2:return {'changed':False,'subsystem':2}
    folder=Path(backup_dir).resolve();folder.mkdir(parents=True,exist_ok=True)
    backup=folder/('ffmpeg_'+digest(original)+'.exe')
    if backup.exists() and backup.read_bytes()!=original:raise ValueError('backup_mismatch')
    if not backup.exists():
        with backup.open('xb') as f:f.write(original)
    receipt=folder/('receipt_'+digest(original)+'.json')
    record={'exe':str(exe),'backup':str(backup),'original_sha256':digest(original),
        'patched_sha256':digest(patched),'subsystem_offset':offset,'original_subsystem':subsystem}
    receipt.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    # In-place two-byte write keeps the existing file attributes and permissions.
    # Windows rejects opening an executable currently used for a merge.
    with exe.open('r+b') as f:
        if digest(f.read())!=record['original_sha256']:raise ValueError('executable_changed_before_write')
        f.seek(offset);f.write(struct.pack('<H',2));f.flush()
    if digest(exe.read_bytes())!=record['patched_sha256']:raise ValueError('post_write_verification_failed')
    return {'changed':True,'subsystem':2,'receipt':str(receipt),'only_subsystem_changed':True}


def restore(receipt):
    record=json.loads(Path(receipt).read_text(encoding='utf-8'))
    exe=Path(record['exe']);original=Path(record['backup']).read_bytes()
    if digest(original)!=record['original_sha256']:raise ValueError('original_backup_changed')
    with exe.open('r+b') as f:
        if digest(f.read())!=record['patched_sha256']:raise ValueError('target_updated_refuse_overwrite')
        f.seek(record['subsystem_offset']);f.write(struct.pack('<H',record['original_subsystem']));f.flush()
    if digest(exe.read_bytes())!=record['original_sha256']:raise ValueError('restore_verification_failed')
    return {'restored':True}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--exe');p.add_argument('--backup-dir');p.add_argument('--apply',action='store_true');p.add_argument('--restore')
    args=p.parse_args()
    if args.restore:result=restore(args.restore)
    elif args.apply:
        if not args.exe or not args.backup_dir:p.error('--apply requires --exe and --backup-dir')
        result=apply(args.exe,args.backup_dir)
    else:
        if not args.exe:p.error('--exe is required for read-only inspection')
        data=Path(args.exe).read_bytes();_,offset,subsystem=gui_image(data)
        result={'subsystem':subsystem,'offset':offset,'sha256':digest(data),'changed':False}
    print(json.dumps(result,ensure_ascii=False))
