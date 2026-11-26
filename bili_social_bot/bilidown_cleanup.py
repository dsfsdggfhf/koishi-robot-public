"""Only files with a bot-created task receipt can be cleaned up. User downloads are excluded."""
from __future__ import annotations
import base64
import json
import time
from pathlib import Path
from .state import BotState
from .utils import save_json_atomic


class OwnedDownloads:
    def __init__(self, cache_dir, cfg):
        self.path=Path(cache_dir).parent/'bilidown_owned_files.json'
        self.cfg=cfg.get('bilidown') or {}
        raw=self.cfg.get('download_dir')
        self.root=Path(raw).resolve() if raw else None
        self.state=BotState(str(self.path))

    def _load(self):
        if not self.path.exists():return {'submissions':{},'files':{},'last_cleanup':0}
        data=json.loads(self.path.read_text(encoding='utf-8-sig'))
        if not isinstance(data.get('submissions'),dict) or not isinstance(data.get('files'),dict):
            raise ValueError('下载所有权记录异常，禁止清理')
        return data

    @staticmethod
    def key(bvid,cid):return f'{bvid}:{int(cid or 0)}'

    def submitted(self,bvid,cid,title):
        if title!=f'Robot_{bvid}_{int(cid or 0)}':return
        with self.state._file_lock():
            data=self._load()
            data['submissions'].setdefault(self.key(bvid,cid),{'title':title,'submitted_at':time.time()})
            save_json_atomic(self.path,data)

    def _safe_path(self,path):
        path=Path(path)
        if not self.root or not path.is_absolute() or path.is_symlink() or path.is_junction():return None
        resolved=path.resolve()
        # Bot-generated Bilidown tasks write directly into the configured download directory.
        # Do not follow nested links, directories, or arbitrary user-configured source roots.
        if resolved.parent!=self.root or not resolved.is_file():return None
        return resolved

    def completed(self,task,path):
        bvid=str(task.get('bvid') or '');cid=int(task.get('cid') or 0)
        title=f'Robot_{bvid}_{cid}'
        if task.get('title')!=title or task.get('status')!='done' or task.get('downloadType')!='merge':return False
        path=self._safe_path(path)
        token=base64.b64encode(str(task.get('id') or '').encode()).decode().rstrip('=')
        if not path or path.name!=f'{title} {token}.mp4':return False
        with self.state._file_lock():
            data=self._load()
            if data['submissions'].get(self.key(bvid,cid),{}).get('title')!=title:return False
            stat=path.stat()
            data['files'].setdefault(str(path),{'task_id':task['id'],'bvid':bvid,'cid':cid,
                'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,'recorded_at':time.time()})
            save_json_atomic(self.path,data)
            return True

    def _unchanged(self,path,receipt):
        safe=self._safe_path(path)
        if not safe:return None
        stat=safe.stat()
        if stat.st_size!=receipt.get('size') or stat.st_mtime_ns!=receipt.get('mtime_ns'):return None
        return safe

    def delete_owned(self,path):
        with self.state._file_lock():
            data=self._load();receipt=data['files'].get(str(Path(path).resolve()))
            if not receipt:return False
            safe=self._unchanged(path,receipt)
            if not safe:return False
            safe.unlink()
            del data['files'][str(safe)]
            save_json_atomic(self.path,data)
            return True

    def cleanup(self,*,dry_run=False,now=None):
        if not self.cfg.get('enabled',False) or not self.cfg.get('cleanup_enabled',True):return []
        stamp=time.time() if now is None else now
        keep=max(24,float(self.cfg.get('cleanup_retention_hours',72) or 72))*3600
        interval=max(300,float(self.cfg.get('cleanup_interval_seconds',3600) or 3600))
        with self.state._file_lock():
            data=self._load()
            if not dry_run and stamp-float(data.get('last_cleanup',0))<interval:return []
            candidates=[]
            for raw,receipt in list(data['files'].items()):
                safe=self._unchanged(raw,receipt)
                if safe and stamp-max(float(receipt.get('recorded_at',stamp)),safe.stat().st_mtime)>=keep:
                    candidates.append(str(safe))
                    if not dry_run:
                        safe.unlink();del data['files'][raw]
            if not dry_run:
                data['last_cleanup']=stamp
                save_json_atomic(self.path,data)
            return candidates
