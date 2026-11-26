"""Validate and enqueue one approved announcement through the existing cloud outbox."""
from datetime import datetime
import hashlib
import hmac
import json
import re
import time

from bilibili_api import user
from .service import ANNOUNCEMENT_LINK, validate_announcement_text


class AnnouncementError(ValueError):
    """An operator-facing reason that does not expose private data."""


def read_manifest(path, private, now):
    path = path.resolve()
    if path.parent != private or path.suffix != '.json':
        raise AnnouncementError('manifest_must_be_in_private_directory')
    with path.open('rb') as handle:
        raw = handle.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise AnnouncementError('manifest_too_large')
    try:
        manifest = json.loads(raw.decode('utf-8-sig'))
        if set(manifest) != {'dynamic_id', 'link', 'template', 'exclude_uids', 'expires_at'}:
            raise ValueError
        dynamic_id = manifest['dynamic_id']
        link = manifest['link']
        template = manifest['template']
        excludes = manifest['exclude_uids']
        expiry = datetime.fromisoformat(manifest['expires_at'])
        if not re.fullmatch(r'[1-9][0-9]{9,19}', dynamic_id):
            raise ValueError
        if not ANNOUNCEMENT_LINK.fullmatch(link):
            raise ValueError
        if re.findall(r'\{[^{}]*\}', template) != ['{nickname}']:
            raise ValueError
        if template.count('{') != 1 or template.count('}') != 1:
            raise ValueError
        if re.findall(r'(?i)https?://[^\s]+', template) != [link]:
            raise ValueError
        if not isinstance(excludes, list) or any(not re.fullmatch(r'[1-9][0-9]{0,19}', uid) for uid in excludes):
            raise ValueError
        if expiry.tzinfo is None:
            raise ValueError
        expires = expiry.timestamp()
        if not now < expires <= now + 48 * 3600:
            raise ValueError
    except (TypeError, ValueError, KeyError) as exc:
        raise AnnouncementError('invalid_announcement_manifest') from exc
    return dynamic_id, link, template, set(excludes), expires, hashlib.sha256(raw).hexdigest()


async def mutual_followings(service):
    info = await service.read_api(lambda: user.get_self_info(service.credential))
    if str(info.get('mid')) != service.me:
        raise AnnouncementError('announcement_account_mismatch')
    profile = user.User(int(service.me), credential=service.credential)
    followings = set()
    mutual = {}
    expected = None
    page_size = 50
    for page in range(1, 201):
        data = await service.read_api(lambda: profile.get_followings(pn=page, ps=page_size))
        if not isinstance(data, dict):
            raise AnnouncementError('invalid_following_page')
        total = data.get('total')
        rows = data.get('list')
        if type(total) is not int or total < 0 or total > page_size * 200:
            raise AnnouncementError('invalid_following_total')
        if expected is None:
            expected = total
        if total != expected:
            raise AnnouncementError('following_total_changed_during_scan')
        if total == 0 and rows is None:
            rows = []
        if not isinstance(rows, list) or len(rows) != min(page_size, total - len(followings)):
            raise AnnouncementError('incomplete_following_page')
        for row in rows:
            if not isinstance(row, dict):
                raise AnnouncementError('invalid_following_row')
            uid = str(row.get('mid'))
            attribute = str(row.get('attribute'))
            if not re.fullmatch(r'[1-9][0-9]{0,19}', uid) or not attribute.isascii() or not attribute.isdecimal():
                raise AnnouncementError('invalid_following_relation')
            if uid == service.me or uid in followings:
                raise AnnouncementError('duplicate_or_self_following')
            followings.add(uid)
            if int(attribute) & 6 == 6:
                name = row.get('uname')
                mutual[uid] = name if isinstance(name, str) else ''
        if len(followings) == total:
            return mutual
    raise AnnouncementError('following_scan_exceeded_page_limit')


def render(template, name, link, forbidden_words):
    name = re.sub(r'[^\w\- ·]', '', name).strip()[:24] or '朋友'
    for nickname in (name, '朋友'):
        body = template.replace('{nickname}', nickname)
        if validate_announcement_text(body, link, forbidden_words):
            return body
    raise AnnouncementError('announcement_body_rejected')


async def prepare(service, path, apply_digest=None, expected_mutual=None):
    now = time.time()
    dynamic_id, link, template, excludes, expires, digest = read_manifest(path, service.private, now)
    if apply_digest is not None:
        if not re.fullmatch(r'[a-f0-9]{64}', apply_digest) or not hmac.compare_digest(digest, apply_digest):
            raise AnnouncementError('manifest_changed_since_preview')
        if type(expected_mutual) is not int or expected_mutual < 0:
            raise AnnouncementError('expected_mutual_count_required')
    store = service.store
    status = {'dynamic_id': dynamic_id, 'sha256': digest, 'enqueued': False, 'queued': 0}
    if store.campaign('announcement', dynamic_id):
        return dict(status, already_exists=True)
    forbidden_words = service.cfg.get('forbidden_words', [])
    render(template, '朋友', link, forbidden_words)
    mutual = await mutual_followings(service)
    if apply_digest is not None and len(mutual) != expected_mutual:
        raise AnnouncementError('mutual_count_changed_since_preview')
    entries = []
    local_excluded = cloud_excluded = 0
    for uid, name in mutual.items():
        if uid in excludes:
            local_excluded += 1
            continue
        if not service.allowed(uid, 'announcement'):
            cloud_excluded += 1
            continue
        name = store.user(uid)['name'] or name
        body = render(template, name, link, forbidden_words)
        entries.append(dict(key=f'announcement:{dynamic_id}:{uid}', uid=uid,
                            kind='announcement', day=dynamic_id, payload={'approved_link': link},
                            body=body, priority=10, expires=expires))
    status.update(mutual=len(mutual), eligible=len(entries), excluded=local_excluded+cloud_excluded,
                  local_excluded=local_excluded, cloud_excluded=cloud_excluded)
    if apply_digest is not None and entries:
        if time.time() >= expires:
            raise AnnouncementError('announcement_expired_during_scan')
        status['enqueued'] = store.create_campaign('announcement', dynamic_id, entries)
        if status['enqueued']:
            status['queued'] = len(entries)
        else:
            status['already_exists'] = True
    return status
