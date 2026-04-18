#!/usr/bin/env python3
"""Migration helper to canonicalise agent model fields in agent/*.md files.

This script will only update files where the model matches a known pattern in
MODEL_CANONICAL_MAP. It will not overwrite models that are not recognised.

Usage:
  scripts/migrate_agent_models.py --path agent --dry-run
  scripts/migrate_agent_models.py --path agent --apply --branch-name <branch>

When --apply is used the script will modify files in-place and write a small
changelog to stdout listing changes.
"""
import argparse
import re
from pathlib import Path
import yaml

MODEL_CANONICAL_MAP = {
    r"github-copilot/gpt-5.2(-codex)?": "github-copilot/gpt-5.2",
    r"proxy/gemma4": "github-copilot/gpt-5.2",
    r"github-copilot/gpt-5-mini": "github-copilot/gpt-5-mini",
}


def extract_front_matter_and_body(text):
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text
    return parts[1], parts[2]


def find_and_map_model(model):
    for pat, canon in MODEL_CANONICAL_MAP.items():
        if re.fullmatch(pat, model):
            return canon
    return None


def process_file(path, apply=False):
    text = Path(path).read_text(encoding='utf-8')
    fm_text, body = extract_front_matter_and_body(text)
    if fm_text is None:
        return None
    data = yaml.safe_load(fm_text) or {}
    model = data.get('model')
    if not model:
        return None
    new_model = find_and_map_model(model)
    if new_model and new_model != model:
        if apply:
            data['model'] = new_model
            new_fm = yaml.safe_dump(data, sort_keys=False).strip() + "\n"
            new_text = '---\n' + new_fm + '---' + body
            Path(path).write_text(new_text, encoding='utf-8')
        return (path, model, new_model)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--path', default='agent')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    p = Path(args.path)
    files = sorted([x for x in p.glob('*.md') if x.is_file()])
    changes = []
    for f in files:
        res = process_file(str(f), apply=args.apply and not args.dry_run)
        if res:
            changes.append(res)

    if not changes:
        print('No files to migrate')
        return

    print('Planned changes:')
    for path, old, new in changes:
        print(f" - {path}: {old} -> {new}")

    if args.apply and not args.dry_run:
        print('\nApplied changes in-place. Please review, commit, and open a PR with per-file notes requesting owner sign-off for any behaviour changes.')

if __name__ == '__main__':
    main()
