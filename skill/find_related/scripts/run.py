from __future__ import annotations

import json
import subprocess
import sys
from typing import Callable, Dict, List, Optional

# The LLM report hook type: accepts a work item dict and a list of candidate ids and returns a string
LLMHook = Callable[[Dict, List[str]], str]


RELATED_MARKERS = ("related-to:", "discovered-from:", "blocked-by:")


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


def _has_related_markers(description: str) -> bool:
    lower = (description or "").lower()
    return any(m in lower for m in RELATED_MARKERS)


def _extract_text(work_item: Dict, key: str) -> str:
    v = work_item.get(key)
    if not v:
        return ""
    return v if isinstance(v, str) else str(v)


def _keywords_from_text(text: str, limit: int = 6) -> List[str]:
    text = (text or "").lower()
    tokens: List[str] = []
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


def _search_worklog(keywords: List[str]) -> List[Dict]:
    found: List[Dict] = []
    for kw in keywords:
        proc = _run(["wl", "list", kw, "--json"])
        if proc.returncode != 0:
            continue
        try:
            payload = json.loads(proc.stdout)
        except Exception:
            continue
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("workItems") or payload.get("items") or []
        else:
            items = []
        for it in items:
            if isinstance(it, dict):
                found.append(it)
    # dedupe by id
    out: List[Dict] = []
    seen = set()
    for i in found:
        iid = i.get("id")
        if iid and iid not in seen:
            seen.add(iid)
            out.append(i)
    return out


def _append_related(description: str, ids: List[str], report: Optional[str]) -> str:
    parts = [description.rstrip()] if description and description.strip() else [""]
    parts.append("")
    for rid in ids:
        parts.append(f"related-to: {rid}")
    if report:
        parts.append("")
        parts.append("## Related work (automated report)")
        parts.append(report.strip())
    return "\n".join(parts).strip() + "\n"


def _update_description(work_id: str, description: str) -> None:
    proc = _run(["wl", "update", work_id, "--description", description, "--json"])
    if proc.returncode != 0:
        raise RuntimeError(f"wl update failed for {work_id}: {proc.stderr.strip()}")


def default_llm_hook(work_item: Dict, candidate_ids: List[str]) -> str:
    # Default hook returns an empty report. Implementations may replace this hook with a real LLM call.
    return ""


def run(
    work_id: str,
    *,
    dry_run: bool = False,
    verbose: bool = False,
    with_report: bool = False,
    llm_hook: LLMHook = default_llm_hook,
) -> Dict:
    work_item = _load_work_item(work_id)
    title = _extract_text(work_item, "title")
    description = _extract_text(work_item, "description")

    if _has_related_markers(description):
        return {
            "found": True,
            "addedIds": [],
            "reportInserted": False,
            "updatedDescription": description,
            "dryRun": dry_run,
        }

    keywords = _keywords_from_text(title + "\n" + description)
    candidates = _search_worklog(keywords)
    candidate_ids = [c.get("id") for c in candidates if c.get("id")]
    candidate_ids = [cid for cid in candidate_ids if cid and cid != work_id]

    report_text = ""
    if with_report:
        if verbose:
            print("Generating LLM-backed related-work report...", file=sys.stderr)
        report_text = llm_hook(work_item, candidate_ids) or ""

    updated_description = description
    report_inserted = False

    if candidate_ids or report_text:
        updated_description = _append_related(description, candidate_ids, report_text)
        if not dry_run:
            if verbose:
                print(
                    f"Updating work item {work_id} with {len(candidate_ids)} related ids",
                    file=sys.stderr,
                )
            _update_description(work_id, updated_description)
            report_inserted = bool(report_text)

    return {
        "found": bool(candidate_ids),
        "addedIds": candidate_ids,
        "reportInserted": report_inserted,
        "updatedDescription": updated_description,
        "dryRun": dry_run,
    }


def main(argv: List[str]) -> int:
    args = [a for a in argv[1:]]
    dry_run = False
    verbose = False
    with_report = False
    work_id: Optional[str] = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--dry-run", "--dryrun"):
            dry_run = True
        elif a in ("--verbose", "-v"):
            verbose = True
        elif a == "--with-report":
            with_report = True
        elif not work_id:
            work_id = a
        i += 1

    if not work_id:
        print("Usage: run.py [--dry-run] [--with-report] [--verbose] <work-item-id>")
        return 2

    result = run(work_id, dry_run=dry_run, verbose=verbose, with_report=with_report)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
