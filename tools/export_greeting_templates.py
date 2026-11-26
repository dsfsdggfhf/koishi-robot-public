"""Export only the two public greeting pools; never export arbitrary runtime data.

Run without arguments to verify snapshots; --write explicitly refreshes them.
Deployment is separate: this tool never replaces live pools or sends messages.
"""
from pathlib import Path
import argparse
import json

ROOT = Path(__file__).resolve().parents[1]
FILES = ('_morning_full_pool.json', '_night_final_pool_93.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='refresh versioned template snapshots')
    args = parser.parse_args()
    exports = []
    for name in FILES:
        source = ROOT / 'data' / name
        if not source.is_file():
            raise SystemExit('Local template source missing; no runtime files were changed: ' + name)
        raw = json.loads(source.read_text(encoding='utf-8-sig'))
        items = raw.get('items')
        if not isinstance(items, list) or not items:
            raise SystemExit('Invalid template pool: ' + name)
        output = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {'name', 'msg'}:
                raise SystemExit('Only name/msg template records can be exported: ' + name)
            if not all(isinstance(item[key], str) and item[key].strip() for key in ('name', 'msg')):
                raise SystemExit('Empty template: ' + name)
            if item['msg'].count('{nickname}') != 1:
                raise SystemExit('Invalid nickname placeholder: ' + name)
            output.append({'name': item['name'], 'msg': item['msg']})
        target = ROOT / 'assets' / 'greeting_templates' / name
        text = json.dumps({'count': len(output), 'items': output}, ensure_ascii=False, indent=2) + '\n'
        exports.append((target, text, len(output)))
    if args.write:
        for target, text, _ in exports:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
    mismatch = False
    for target, text, count in exports:
        matches = target.is_file() and target.read_text(encoding='utf-8') == text
        print(f'{target.name}: {count} templates; snapshot matches={matches}')
        mismatch |= not matches
    return 1 if mismatch else 0


if __name__ == '__main__':
    raise SystemExit(main())
