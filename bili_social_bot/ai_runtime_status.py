"""Health evidence from real business calls; no probes, prompts or credentials."""
from __future__ import annotations
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit


class EndpointRedirectError(RuntimeError):
    """An API route returned a redirect rather than a completion."""


def call_kind(payload: dict) -> str:
    for message in payload.get('messages') or []:
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list) and any(isinstance(x, dict) and x.get('type') in {'image_url', 'input_image'} for x in content):
            return 'vision'
    return 'text'

def error_category(error: Exception | None) -> str:
    if error is None:
        return ''
    text = str(error).lower()
    if isinstance(error, EndpointRedirectError):
        return 'endpoint_redirect'
    if '旧版 api key' in text and '绑定渠道' in text:
        return 'legacy_route_key'
    if text.startswith('http 5'):
        return 'upstream_error'
    if 'empty_response' in text:
        return 'empty_response'
    if 'insufficient balance' in text or '余额不足' in text:
        return 'balance'
    if 'free tier' in text or 'freetiererror' in text or 'active opencode go subscription' in text or '401' in text:
        return 'authorization'
    if '429' in text or 'rate limit' in text:
        return 'rate_limit'
    if 'timeout' in type(error).__name__.lower() or 'timed out' in text:
        return 'timeout'
    if '模型已停用' in text:
        return 'model_unavailable'
    if '400' in text or 'distillation' in text or 'heartbeat probing' in text:
        return 'request_rejected'
    if '403' in text:
        return 'authorization'
    return 'network_or_service'

def record_event(path: Path, *, kind: str, provider: str, model: str, success: bool,
                 error: Exception | None = None, now: float | None = None) -> None:
    event = {'time': time.time() if now is None else now, 'kind': kind,
             'provider': urlsplit(provider).hostname or '', 'model': model,
             'status': 'success' if success else 'error', 'error_category': error_category(error)}
    # One append write per short record; never store a payload or an error body.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(event, ensure_ascii=False, separators=(',', ':'))+'\n').encode('utf-8'))
        finally:
            os.close(fd)
    except OSError:
        pass  # Telemetry failure must not make a delivered business call fail.

def recent_status(path: Path, kind: str, *, now: float | None = None, max_age: float = 10800) -> dict:
    now = time.time() if now is None else now
    latest = {}
    try:
        with path.open('rb') as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - 256 * 1024)
            handle.seek(start)
            if start:
                handle.readline()
            lines = handle.read().decode('utf-8', errors='replace').splitlines()
        for line in lines:
            try:
                row = json.loads(line)
                stamp = float(row.get('time', 0))
                if row.get('kind') != kind or not 0 <= now-stamp <= max_age:
                    continue
                key = str(row.get('provider','')) + '/' + str(row.get('model',''))
                if stamp >= float(latest.get(key,{}).get('time',0)):
                    latest[key] = row
            except (ValueError, TypeError):
                continue
    except OSError:
        pass
    if not latest:
        return {'state':'unverified', 'usable':None, 'categories':[]}
    successful = [r for r in latest.values() if r.get('status')=='success']
    errors = [r for r in latest.values() if r.get('status')=='error']
    return {'state':('degraded' if errors else 'healthy') if successful else 'failed',
            'usable':bool(successful),
            'categories':sorted({r.get('error_category','unknown') for r in errors}),
            'last_event_at':max(float(r['time']) for r in latest.values())}
