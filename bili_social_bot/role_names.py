"""Exact character-name normalization for evidence; never replace prose substrings."""
from __future__ import annotations

import re

from .textnorm import ROMAJI_TO_CN, normalize_traditional

# Unify spellings already present in the local library and the WD vocabulary.
_DISPLAY_NAMES = {
    '蕾米莉亚·斯卡雷特': '蕾米莉亚·斯卡蕾特',
    '芙兰朵露·斯卡雷特': '芙兰朵露·斯卡蕾特',
    '蕾蒂·怀特洛克': '蕾蒂·霍瓦特洛克',
    '安娜贝尔·卡娜': '卡娜·安娜贝拉尔',
    '莉莉白': '莉莉霍瓦特',
}
_EXACT_NAMES = dict(ROMAJI_TO_CN, **{
    'hoshiguma yuugi': '星熊勇仪', 'yuugi hoshiguma': '星熊勇仪',
    'hatsune miku': '初音未来', '初音ミク': '初音未来',
})


def name_key(value: str) -> str:
    text = normalize_traditional(str(value or '')).translate(str.maketrans('儀諏訪（）', '仪诹访()'))
    return re.sub(r'\s+', ' ', text.replace('_', ' ')).strip().casefold()


def display_name(value: str) -> str:
    value = normalize_traditional(str(value or '')).strip()
    value = _EXACT_NAMES.get(name_key(value), value)
    return _DISPLAY_NAMES.get(value, value)


def role_alias_groups(aliases: dict) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name, values in aliases.items():
        canonical = display_name(name)
        if not canonical:
            continue
        group = groups.setdefault(canonical, [])
        for alias in [str(name), canonical, *values]:
            alias = str(alias).strip()
            if len(alias) >= 2 and alias not in group:
                group.append(alias)
    for alias, name in _EXACT_NAMES.items():
        canonical = display_name(name)
        group = groups.setdefault(canonical, [canonical])
        for variant in (alias, alias.replace(' ', '_')):
            if variant not in group:
                group.append(variant)
    for old, canonical in _DISPLAY_NAMES.items():
        group = groups.setdefault(canonical, [canonical])
        if old not in group:
            group.append(old)
    for alias in ('初音', '初音ミク'):
        if alias not in groups['初音未来']:
            groups['初音未来'].append(alias)
    return groups


def role_alias_map(aliases: dict) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for canonical, values in role_alias_groups(aliases).items():
        for value in [canonical, *values]:
            candidates.setdefault(name_key(value), set()).add(canonical)
    # Ambiguous aliases cannot cast a vote for whichever entry happens to be last.
    return {key: next(iter(names)) for key, names in candidates.items() if key and len(names) == 1}


def canonical_role(label: str, aliases: dict[str, str]) -> str:
    name = str(label or '').strip().strip('*「」『』【】`')
    name = re.sub(r'^(?:角色名|角色)\s*[:：]\s*', '', name)
    if not name or any(part in name for part in ('疑似', '可能', '不确定', '无法', '、', '，', '/', '或')):
        return ''
    key = name_key(name)
    if key in aliases:
        return aliases[key]
    # Only known franchise/edition qualifiers may be removed. Arbitrary suffixes
    # could identify a different character with the same name.
    match = re.fullmatch(r'(.+?)\s*\((touhou|pc-98|vocaloid)\)', key)
    if match:
        base, franchise = match.groups()
        canonical = aliases.get(base, '')
        if franchise == 'vocaloid':
            return canonical if canonical == '初音未来' else ''
        return canonical if canonical and canonical != '初音未来' else ''
    return ''
