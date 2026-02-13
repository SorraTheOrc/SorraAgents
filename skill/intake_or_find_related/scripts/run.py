from __future__ import annotations

import json
import subprocess
import sys
from typing import Dict, List


RELATED_MARKERS = (
    "related-to:",
    "discovered-from:",
    "blocked-by:",
    "blocks:",
)


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _load_work_item(work_id: str) -> Dict:
    proc = _run(["wl", "show", work_id, "--json"])
    if proc.returncode != 0:
        raise RuntimeError(f"wl show failed for {work_id}: {proc.stderr.strip()}")
    payload = json.loads(proc.stdout)
    if isinstance(payload, dict) and "workItem" in payload:
        return payload["workItem"] or {}
    return payload


def _extract_text(work_item: Dict, key: str) -> str:
    value = work_item.get(key)
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _intake_required(work_item: Dict) -> bool:
    stage = _extract_text(work_item, "stage").strip().lower()
    description = _extract_text(work_item, "description").strip()
    return stage == "idea" or not description


def _extract_keywords(title: str, description: str, limit: int = 6) -> List[str]:
    text = f"{title}\n{description}".lower()
    tokens = []
    for raw in text.replace("/", " ").replace("-", " ").split():
        token = "".join(ch for ch in raw if ch.isalnum())
        if len(token) < 4:
            continue
        if token in {"summary", "acceptance", "criteria", "user", "story"}:
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens


def _search_related(keywords: List[str]) -> List[Dict]:
    related: List[Dict] = []
    for keyword in keywords:
        proc = _run(["wl", "list", keyword, "--json"])
        if proc.returncode != 0:
            continue
        try:
            payload = json.loads(proc.stdout)
        except Exception:
            continue
        items: List[Dict]
        if isinstance(payload, dict):
            items = payload.get("workItems") or payload.get("items") or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        for item in items:
            if isinstance(item, dict):
                related.append(item)
    seen = set()
    unique: List[Dict] = []
    for item in related:
        item_id = item.get("id")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        unique.append(item)
    return unique


def _has_related_markers(description: str) -> bool:
    lower = description.lower()
    return any(marker in lower for marker in RELATED_MARKERS)


def _append_related(description: str, related_ids: List[str]) -> str:
    lines = [description.rstrip(), ""] if description.strip() else []
    for rid in related_ids:
        lines.append(f"related-to: {rid}")
    return "\n".join(lines).strip() + "\n"


def _update_description(work_id: str, description: str) -> None:
    proc = _run(["wl", "update", work_id, "--description", description, "--json"])
    if proc.returncode != 0:
        raise RuntimeError(f"wl update failed for {work_id}: {proc.stderr.strip()}")


def run(work_id: str, *, dry_run: bool = False, verbose: bool = False) -> Dict:
    """Ensure related context for work_id.

    Returns a JSON-serializable dict summarizing actions. Use dry_run=True to avoid
    making side-effects (no `wl update` or `opencode run`).
    """
    work_item = _load_work_item(work_id)
    title = _extract_text(work_item, "title")
    description = _extract_text(work_item, "description")

    if _intake_required(work_item):
        if dry_run:
            # In dry-run mode we report that intake would be performed but do not run it.
            return {
                "intakePerformed": False,
                "relatedFound": False,
                "updatedDescription": _extract_text(work_item, "description"),
                "addedRelatedIds": [],
                "dryRun": True,
            }
        if verbose:
            print(f"Intake required for {work_id}; running intake...", file=sys.stderr)
        proc = _run(["opencode", "run", f"/intake {work_id}"])
        if proc.returncode != 0:
            raise RuntimeError(f"intake failed for {work_id}: {proc.stderr.strip()}")
        work_item = _load_work_item(work_id)
        description = _extract_text(work_item, "description")
        return {
            "intakePerformed": True,
            "relatedFound": _has_related_markers(description),
            "updatedDescription": description,
            "addedRelatedIds": [],
            "dryRun": False,
        }

    if _has_related_markers(description):
        return {
            "intakePerformed": False,
            "relatedFound": True,
            "updatedDescription": description,
            "addedRelatedIds": [],
            "dryRun": dry_run,
        }

    keywords = _extract_keywords(title, description)
    related_items = _search_related(keywords)
    related_ids = [item.get("id") for item in related_items if item.get("id")]
    related_ids = [
        rid for rid in related_ids if isinstance(rid, str) and rid != work_id
    ]

    if related_ids:
        updated_description = _append_related(description, related_ids)
        if not dry_run:
            if verbose:
                print(
                    f"Updating work item {work_id} description with {len(related_ids)} related ids",
                    file=sys.stderr,
                )
            _update_description(work_id, updated_description)
        description = updated_description
    else:
        updated_description = description

    return {
        "intakePerformed": False,
        "relatedFound": bool(related_ids),
        "updatedDescription": updated_description,
        "addedRelatedIds": related_ids,
        "dryRun": dry_run,
    }


def main(argv: List[str]) -> int:
    # Basic arg parsing: support --dry-run and --verbose flags
    dry_run = False
    verbose = False
    args = [a for a in argv[1:]]
    work_id = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--dry-run", "--dryrun"):
            dry_run = True
        elif a in ("--verbose", "-v"):
            verbose = True
        elif not work_id:
            work_id = a
        else:
            # ignore extras
            pass
        i += 1

    if not work_id:
        print("Usage: run.py [--dry-run] [--verbose] <work-item-id>")
        return 2

    result = run(work_id, dry_run=dry_run, verbose=verbose)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
