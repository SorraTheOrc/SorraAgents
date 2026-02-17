"""Generate a scripted AMPA/PATCH conversation and optionally run audit."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime

try:
    import opencode_ai
    from opencode_ai import Opencode
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: opencode_ai. Install with 'pip install --pre opencode-ai'"
    ) from exc


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _display_base_url(base_url: str | None) -> str:
    if not base_url:
        return "unknown"
    return base_url


class ConversationManager:
    def __init__(
        self,
        name: str,
        seed: str,
        policy: str,
        client: Opencode,
        provider_id: str,
        model_id: str,
        base_url: str | None,
        verbose: bool,
    ) -> None:
        self.name = name
        if verbose:
            print(
                f"[{self.name}] creating session provider={provider_id} "
                f"model={model_id} base_url={_display_base_url(base_url)}"
            )
        self.session = client.session.create(extra_body={})
        self.session_id = self.session.id
        self.last_message = seed
        self.policy = policy
        self.provider_id = provider_id
        self.model_id = model_id
        self.client = client
        self.verbose = verbose

    def respond(
        self, incoming: str, turn: int, *, policy_override: str | None = None
    ) -> str:
        policy = policy_override if policy_override is not None else self.policy
        prompt = (
            f"{incoming}\n\n{policy}\n\nRespond briefly as {self.name}."
            if policy
            else f"{incoming}\n\nRespond briefly as {self.name}."
        )
        try:
            self.client.session.chat(
                self.session_id,
                provider_id=self.provider_id,
                model_id=self.model_id,
                parts=[{"type": "text", "text": prompt}],
            )
            messages = self.client.session.messages(self.session_id)
        except opencode_ai.APIError as exc:
            raise SystemExit(f"OpenCode API error during chat: {exc}") from exc

        last = messages[-1] if messages else None
        reply = _extract_text(getattr(last, "parts", None)) if last is not None else ""
        if not reply:
            reply = "(no assistant reply)"
        if self.verbose and self.name != "AMPA":
            print(f"[{self.name}] message: {reply}")
        return reply


def _extract_text(parts) -> str:
    texts = []
    for part in parts or []:
        try:
            if getattr(part, "type", None) == "text":
                text = getattr(part, "text", None)
                if text:
                    texts.append(text)
        except Exception:
            continue
    return "\n".join(texts).strip()


def _is_question(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    if "?" in text:
        return True
    return lowered.startswith("question:")


def _is_completion(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    triggers = [
        "work is complete",
        "work completed",
        "completed my work",
        "implementation complete",
        "implementation completed",
        "done",
        "finished",
        "ready for review",
    ]
    if any(t in lowered for t in triggers):
        return True
    return re.search(r"https?://github\.com/[^\s]+/pull/\d+", text, re.I) is not None


def _summarize_transcript(
    client: Opencode,
    transcript_path: str,
    provider_id: str,
    model_id: str,
    verbose: bool,
) -> None:
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            log_text = f.read().strip()
    except Exception as exc:
        raise SystemExit(f"Failed to read transcript for summary: {exc}") from exc

    session = client.session.create(extra_body={})
    session_id = session.id
    prompt = (
        "Summarize the conversation and actions in the following conversation-log:\n\n"
        f"{log_text}"
    )
    try:
        client.session.chat(
            session_id,
            provider_id=provider_id,
            model_id=model_id,
            parts=[{"type": "text", "text": prompt}],
        )
        messages = client.session.messages(session_id)
    except opencode_ai.APIError as exc:
        raise SystemExit(f"OpenCode API error during summary chat: {exc}") from exc

    last = messages[-1] if messages else None
    summary = _extract_text(getattr(last, "parts", None)) if last is not None else ""
    if not summary:
        summary = "(no summary reply)"
    if verbose:
        print(f"[summary] message: {summary}")
    print(summary)


def _log(message: str, *, verbose: bool) -> None:
    if verbose:
        print(message)


def _log_json(payload: dict[str, object], *, verbose: bool) -> None:
    if verbose:
        print(json.dumps(payload, ensure_ascii=False))


def _write_transcript(entries: list[dict[str, str]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _run_audit(work_item_id: str, *, verbose: bool) -> subprocess.CompletedProcess:
    cmd = f'opencode run "/audit {work_item_id}"'
    _log(f"[audit] running: {cmd}", verbose=verbose)
    return subprocess.run(
        cmd,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )


def _audit_recommends_close(text: str) -> bool:
    if not text:
        return False
    patterns = [
        r"ready to close",
        r"can be closed",
        r"recommend(?:ed)?\s+to\s+close",
        r"should be closed",
        r"closure recommended",
    ]
    return any(re.search(p, text, re.I) for p in patterns)


def _update_work_item(work_item_id: str, apply_update: bool, *, verbose: bool) -> str:
    cmd = (
        f"wl update {work_item_id} --status completed --stage in_review "
        "--needs-producer-review true --json"
    )
    if apply_update:
        _log(f"[wl] applying update: {cmd}", verbose=verbose)
        subprocess.run(cmd, shell=True, check=False)
        return "Applied: " + cmd
    _log(f"[wl] suggested update: {cmd}", verbose=verbose)
    return "Suggested: " + cmd


def _create_temp_work_item(*, verbose: bool) -> str:
    title = "DELETE ME: test work item"
    description = (
        "Create a hello_world.md file and insert a joke about lazy developers in it"
    )
    cmd = (
        "wl create --title "
        f'"{title}" --description "{description}" '
        "--priority low --issue-type task --json"
    )
    _log(f"[wl] creating temp work item: {cmd}", verbose=verbose)
    proc = subprocess.run(
        cmd,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"Failed to create temp work item: {proc.stderr.strip()}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception as exc:
        raise SystemExit("Failed to parse wl create output") from exc
    work_item = payload.get("workItem") or payload.get("work_item") or payload
    work_id = work_item.get("id") if isinstance(work_item, dict) else None
    if not work_id:
        raise SystemExit("wl create did not return a work item id")
    return str(work_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-item")
    parser.add_argument("--out", default="ampa_patch_transcript.jsonl")
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--run-audit", action="store_true", default=True)
    parser.add_argument("--apply-update", action="store_true", default=True)
    parser.add_argument(
        "--provider-ampa",
        default=os.getenv("OPENCODE_PROVIDER_ID_AMPA", "LLama"),
    )
    parser.add_argument(
        "--provider-patch",
        default=os.getenv("OPENCODE_PROVIDER_ID_PATCH", "Github Copilot"),
    )
    parser.add_argument(
        "--model-ampa",
        default=os.getenv("OPENCODE_MODEL_ID_AMPA", "GPT-OSS 120b (local)"),
    )
    parser.add_argument(
        "--model-patch",
        default=os.getenv("OPENCODE_MODEL_ID_PATCH", "GPT-5-mini"),
    )
    parser.add_argument(
        "--base-url", default=os.getenv("OPENCODE_BASE_URL", "http://localhost:9999")
    )
    parser.add_argument("--verbose", action="store_true", default=True)
    args = parser.parse_args()

    work_item_id = args.work_item or _create_temp_work_item(verbose=args.verbose)

    client = Opencode(base_url=args.base_url) if args.base_url else Opencode()
    try:
        ampa = ConversationManager(
            "AMPA",
            (
                f"Implement {work_item_id} using the implement skill. "
                "You must only respond with either a) a clear question or b) "
                "confirmation that you have completed your work."
            ),
            "Answer questions directly. When work is complete, confirm and proceed to update the work item.",
            client,
            args.provider_ampa,
            args.model_ampa,
            args.base_url,
            args.verbose,
        )
        patch = ConversationManager(
            "PATCH",
            "Acknowledged. I will start by asking clarifying questions.",
            "Ask clear questions or confirm completion when done.",
            client,
            args.provider_patch,
            args.model_patch,
            args.base_url,
            args.verbose,
        )
    except opencode_ai.APIConnectionError as exc:
        hint = "Set OPENCODE_BASE_URL to your running OpenCode API (e.g. http://localhost:8083)."
        raise SystemExit(f"Connection error. {hint}") from exc
    except opencode_ai.APIStatusError as exc:
        hint = "The API rejected session creation. Ensure your OpenCode server supports POST /session."
        raise SystemExit(f"Request error. {hint}") from exc

    transcript = []
    current_sender = ampa
    other = patch
    message = ampa.last_message

    _log(
        f"[AMPA] conversation start work_item={work_item_id}",
        verbose=args.verbose,
    )

    for turn in range(1, args.rounds * 2 + 1):
        entry = {
            "timestamp": _now_iso(),
            "turn": turn,
            "sender": current_sender.name,
            "session_id": current_sender.session_id,
            "provider_id": current_sender.provider_id,
            "model_id": current_sender.model_id,
            "message": message,
        }
        transcript.append(entry)
        _log_json(entry, verbose=args.verbose)

        if current_sender.name == "PATCH" and _is_completion(message):
            _log(
                "[AMPA] completion signal from PATCH; updating work item and ending conversation",
                verbose=args.verbose,
            )
            _update_work_item(work_item_id, args.apply_update, verbose=args.verbose)
            break

        policy_override = None
        if other.name == "AMPA":
            if _is_completion(message):
                _log(
                    "[AMPA] detected completion signal; updating work item status",
                    verbose=args.verbose,
                )
                _update_work_item(work_item_id, args.apply_update, verbose=args.verbose)
                policy_override = (
                    "Confirm completion and note the work item was updated to completed/in_review "
                    "with needs-producer-review."
                )
            elif _is_question(message):
                policy_override = "Answer the question directly and concisely."
        response = other.respond(message, turn, policy_override=policy_override)
        message = response
        current_sender, other = other, current_sender

    _write_transcript(transcript, args.out)
    print(f"Wrote {len(transcript)} messages to {args.out}")
    _summarize_transcript(
        client, args.out, args.provider_ampa, args.model_ampa, args.verbose
    )

    if not args.run_audit:
        print("Skipping audit. Use --run-audit to execute /audit.")
        return

    proc = _run_audit(work_item_id, verbose=args.verbose)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        print("Audit failed:")
        print(output.strip())
        return

    print("Audit output:")
    print(output.strip())

    if _audit_recommends_close(output):
        print(_update_work_item(work_item_id, args.apply_update, verbose=args.verbose))
    else:
        print(
            "Audit did not recommend closure. Document gaps and next steps in the work item."
        )


if __name__ == "__main__":
    main()
