"""Local role verdicts and evidence consistency. No model/network calls."""
from __future__ import annotations

import re
import math
from typing import Callable

from .role_names import canonical_role, role_alias_groups, role_alias_map

IDENTITY_HEADER = '【角色最终结论】'
IDENTITY_VERSION = 2
ROLE_POLICY_VERSION = 'exact-names-evidence-guard-v2'
# These are useful metadata hints, but ordinary prose also uses them literally.
_CONTEXT_ONLY_ALIASES = {'黑白', '宵暗', '厄神', '梦消失', '夢消失', 'lost dream',
                         'scarlet', 'medicine', 'melancholy', 'luna', 'lily'}


def parse_frame_roles(first_line: str, canonicalize: Callable[[str], str]) -> list[tuple[str, str]]:
    """Parse separately scored people; prose or unscored lists are not votes."""
    result: dict[str, str] = {}
    # Only the leading block of scored records is evidence. Stop at explanatory
    # prose, so a comparison/name mentioned later cannot become another vote.
    for line in str(first_line or '').strip().splitlines()[:8]:
        line = re.sub(r'^\s*(?:[-*]\s+|\d+[.)、]\s*)', '', line).strip().strip('*')[:400]
        pieces = re.split(r'[；;]', line)
        matches = [re.fullmatch(r'\s*(.{1,60}?)\s*[｜|]\s*(高|中|低)(?:置信度)?\s*[。.：:]?\s*', p)
                   for p in pieces if p.strip()]
        if not matches or any(m is None for m in matches):
            break
        if any(re.search(r'[:：]', m[1]) and not re.match(r'^(?:角色名|角色)\s*[:：]', m[1]) for m in matches):
            break
        for match in matches[:6]:
            if match[2] == '低':
                continue
            name = canonicalize(match[1])
            if name and (name not in result or match[2] == '高'):
                result[name] = match[2]
    return list(result.items())[:6]


def additional_roles(votes: dict, high_votes: dict, local_votes: dict,
                     declared: list[str], coobserved: set[tuple[str, str]], primary: list[str]) -> list[str]:
    """Keep a second visible person without converting every tag to a person."""
    result = list(dict.fromkeys(primary))
    # A single tag/high-score local prediction alone never adds another person.
    for name, count in votes.items():
        if name in result:
            continue
        independent = count >= 2 or (count >= 1 and local_votes.get(name, 0) >= 1)
        together = any(tuple(sorted((name, p))) in coobserved for p in primary)
        paired = (bool(primary) and count >= 1 and high_votes.get(name, 0) >= 1
                  and name in declared and together)
        # Different single-person predictions may be disagreement, not two people.
        if paired or (independent and together):
            result.append(name)
    return result[:6]


def alias_groups(aliases: dict) -> dict[str, list[str]]:
    return role_alias_groups(aliases)


def role_policy(cfg: dict) -> dict:
    return {'version': ROLE_POLICY_VERSION,
            'enabled': bool(cfg.get('character_vote_enabled', True)),
            'visual_frames': int(cfg.get('character_vote_frames', 3) or 3),
            'local_frames': int(cfg.get('character_wd_frames', 8) or 8),
            'min_score': float(cfg.get('wd_min_score', .6) or .6)}


def local_frame_candidates(hits: list, aliases: dict, min_score: float) -> list[dict]:
    """Keep top-k evidence, but only a known primary hit can cast one frame vote."""
    candidates = []
    for rank, hit in enumerate(list(hits or [])[:3], 1):
        try:
            tag, raw_score = hit
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        name = canonical_role(str(tag), aliases)
        valid_score = math.isfinite(score) and 0 <= score <= 1
        reason = ('invalid_score' if not valid_score else 'unmapped_tag' if not name
                  else 'low_score' if score < min_score else 'secondary_candidate' if rank > 1
                  else 'counted_primary')
        candidates.append({'tag': str(tag), 'name': name, 'rank': rank,
                           'score': score if valid_score else None, 'reason': reason})
    return candidates


def _contains(text: str, alias: str) -> bool:
    if not alias or len(alias) < 2:
        return False
    if alias.isascii():
        return bool(re.search(r'(?<![A-Za-z0-9_])' + re.escape(alias) + r'(?![A-Za-z0-9_])', text, re.I))
    return alias.lower() in text.lower()


def make_identity(confirmed: list[str], aliases: dict, observations: str = '',
                  uncertain: list[str] | None = None, source: str = 'existing-votes') -> dict:
    groups = alias_groups(aliases)
    mapping = role_alias_map(aliases)
    confirmed = list(dict.fromkeys(n for label in confirmed if (n := canonical_role(label, mapping))))[:6]
    uncertain = [n for n in dict.fromkeys(canonical_role(label, mapping) for label in (uncertain or []))
                 if n and n not in confirmed][:6]
    checked_groups = {name: [a for a in values if a.casefold() not in _CONTEXT_ONLY_ALIASES
                            and canonical_role(a, mapping) == name] for name, values in groups.items()}
    observed = [name for name, values in checked_groups.items() if any(_contains(observations, a) for a in values)]
    unsupported = [n for n in observed if n not in confirmed]
    return {'version': IDENTITY_VERSION, 'confirmed': confirmed, 'uncertain': uncertain,
            'unsupported': unsupported, 'source': source,
            'assertion_aliases': checked_groups,
            'blocked_aliases': list(dict.fromkeys(a for name in unsupported for a in checked_groups.get(name, [name])
                                                if a.lower() not in {'恋恋', 'koishi', 'こいし'}))[:180]}


def identity_context(identity: object, max_chars: int = 340) -> str:
    if not isinstance(identity, dict) or identity.get('version') not in (1, IDENTITY_VERSION):
        return ''
    confirmed = '、'.join(identity.get('confirmed') or [])
    uncertain = '、'.join(identity.get('uncertain') or [])
    # Do not put rejected names in the generation prompt: the deny list is checked locally.
    line = IDENTITY_HEADER + ('已确认：' + confirmed if confirmed else '身份未确认')
    if uncertain:
        line += '；待核验：' + uncertain
    line += '。同框分别描述；只对已确认人物断言身份。旧描述中的身份、发型或衣装解释若冲突不可沿用，不编造换装理由。'
    return line[:max(0, max_chars)]


def consistent_observation(text: str, identity: object) -> str:
    """Discard conflicting paragraphs instead of changing a person's name in them."""
    if not isinstance(identity, dict) or not identity.get('blocked_aliases'):
        return str(text or '')
    forbidden = identity['blocked_aliases']
    paragraphs = re.split(r'\n\s*\n', str(text or ''))
    return '\n\n'.join(p for p in paragraphs if not any(_contains(p, a) for a in forbidden)).strip()


def _non_assertive_mention(before: str, after: str, clause: str) -> bool:
    # Scope exceptions to this occurrence; a comparison in one clause must not
    # excuse a later assertion about a different person.
    if re.search(r'(?:不是|并非|不像|没有|无法确认(?:是)?|不能确认(?:是)?|不确定(?:是否|是不是|是)?|'
                 r'疑似|可能是|也许是|大概是|像是|有点像|像|让我想起|让我想到|想起了|想到了|'
                 r'提到了|说起|关于|类似于|致敬)\s*[“「《]?$', before):
        return True
    if re.search(r'(?:我|恋恋|咱)(?:也|很|一直)?(?:喜欢|想念|想起)\s*$', before):
        return True
    if re.fullmatch(r'\s*[”」》]?(?:吗|么)?\s*[?？]\s*', after):
        return True
    depicted = re.search(r'画面|图中|图里|视频中|视频里|镜头|左边|右边|屏幕', clause)
    return bool(not depicted and re.search(r'设定|原作|能力|初登场|官方资料|游戏剧情', clause))


def output_role_conflict(text: str, identity: object) -> bool:
    if not isinstance(identity, dict):
        return False
    groups = identity.get('assertion_aliases') or {}
    confirmed = set(identity.get('confirmed') or [])
    blocked = set(identity.get('blocked_aliases') or [])
    # Legacy callers keep the prior deny-list until their identity is upgraded.
    if not groups:
        return any(_contains(str(text or ''), a) for a in blocked)
    from .textnorm import normalize_traditional
    for clause in re.split(r'[，,。！!；;\n]', normalize_traditional(str(text or ''))):
        mentions = []
        for name, aliases in groups.items():
            for alias in aliases:
                if len(alias) < 2:
                    continue
                pattern = re.escape(normalize_traditional(alias))
                if alias.isascii():
                    pattern = r'(?<![A-Za-z0-9_])' + pattern + r'(?![A-Za-z0-9_])'
                for match in re.finditer(pattern, clause, re.I):
                    mentions.append((match.start(), match.end(), name, alias))
        handled_spans = []
        # Longest known name wins even across characters: 露娜萨 must not be
        # interpreted as another character's shorter alias 露娜.
        for start, end, name, alias in sorted(mentions, key=lambda m: m[1] - m[0], reverse=True):
            if any(left <= start and end <= right for left, right in handled_spans):
                continue
            handled_spans.append((start, end))
            if name in confirmed:
                continue
            before, after = clause[:start], clause[end:]
            if _non_assertive_mention(before, after, clause):
                continue
            visual = re.search(r'(?:画面|图中|图里|视频中|视频里|镜头|左边|右边|屏幕).{0,18}$', before)
            if not visual and name == '古明地恋' and alias.casefold() in {'恋恋', 'koishi', 'こいし'} \
                    and re.match(r'\s*(?:觉得|感觉|想|来|今天|在这里|在这儿|陪你|谢谢|看得|也)', after):
                continue
            named = re.search(r'(?:这|那|她|他|它|主角|角色)(?:位|个|只)?(?:角色|人|少女|女孩|人物)?(?:就是|是|叫)\s*$', before)
            action = re.match(r'\s*(?:正在|在|正|跳舞|起舞|抬手|挥手|转身|走来|站着|坐着|穿着|戴着|拿着|抱着|唱歌|好可爱|真可爱|的[浅深淡灰粉红橙黄绿青蓝紫黑白金银棕褐色长短]{0,5}(?:头发|发色|帽子|裙子|衣服|衣装|动作|翅膀)|(?:和|与|跟).{1,18}?(?:一起|一同)(?:跳舞|起舞|出现|抬手|唱歌))', after)
            alone = not before.strip(' “「《') and not after.strip(' ”」》呀啊呢~～')
            if alias in blocked or visual or named or action or alone:
                return True
    return False


def cached_identity(identity: object, aliases: dict, observations: str, cfg: dict | None = None) -> dict:
    """Upgrade locally; uncertain policy/evidence never forces new paid analysis."""
    old = identity if isinstance(identity, dict) else {}
    confirmed = list(old.get('confirmed') or [])
    uncertain = list(old.get('uncertain') or [])
    policy_changed = cfg is not None and old.get('policy') != role_policy(cfg)
    if policy_changed:
        uncertain = confirmed + uncertain
        confirmed = []
    source = 'cached-policy-needs-verification' if policy_changed else old.get('source', 'cached-unverified')
    result = make_identity(confirmed, aliases,
        observations + '\n' + ' '.join(old.get('blocked_aliases') or []), uncertain, source)
    if old.get('evidence'):
        result['evidence'] = old['evidence']
    if old.get('policy'):
        result['policy'] = old['policy']
    if policy_changed:
        result['requested_policy'] = role_policy(cfg)
    return result


def apply_identity(analysis: object, identity: dict) -> None:
    analysis.role_identity = identity
    for field in ('vision_text', 'premium_vision_text', 'ai_conclusion_text'):
        setattr(analysis, field, consistent_observation(getattr(analysis, field, ''), identity))
    for field in ('summary', 'comment_draft', 'share_reason'):
        if output_role_conflict(getattr(analysis, field, ''), identity):
            setattr(analysis, field, '')


def role_candidate_metadata(text: str) -> str:
    """An uploader's nickname is attribution, not a depicted-character hint."""
    return '\n'.join(line for line in str(text or '').splitlines()
                     if not re.match(r'\s*(?:UP主|作者|投稿者)\s*[:：]', line))


def verification_images(paths: list[str], max_images: int, output_dir, multi_person: bool = False) -> list[str]:
    """Use the existing image budget for full frames plus local detail views."""
    from pathlib import Path
    import hashlib
    valid = list(dict.fromkeys(str(p) for p in paths if Path(p).is_file()))
    budget = min(max(1, int(max_images)), len(valid))
    if len(valid) <= budget:
        baseline = valid[:budget]
    else:
        baseline = [valid[round(i*(len(valid)-1)/max(1,budget-1))] for i in range(budget)]
    if budget < 3:
        return baseline
    try:
        from PIL import Image, ImageOps
        original = Path(baseline[0])
        with Image.open(original) as opened:
            if opened.width*opened.height > 30_000_000:
                return baseline
            picture = ImageOps.exif_transpose(opened).convert('RGB')
        width, height = picture.size
        if min(width, height) < 160:
            return baseline
        if multi_person and width > height*1.2:
            boxes = [(0,0,int(width*.58),height),(int(width*.42),0,width,height)]
        else:
            # Keep the whole height: hats, chest eyes and clothing remain visible.
            boxes = [(int(width*.18),0,int(width*.82),height)]
        folder = Path(output_dir)
        folder.mkdir(parents=True,exist_ok=True)
        key = hashlib.sha256(original.read_bytes()).hexdigest()[:16]
        crops=[]
        for index,box in enumerate(boxes):
            crop=picture.crop(box)
            crop.thumbnail((960,960))
            target=folder/f'{key}-detail-{index}.jpg'
            if not target.exists(): crop.save(target,'JPEG',quality=90)
            crops.append(str(target))
        # Always retain the original; every crop replaces an existing input slot.
        return [baseline[0]] + crops[:budget-1] + baseline[1:budget-len(crops)]
    except Exception:
        return baseline
