"""Simple terminal-based conversation client using the OpenCode Python SDK."""

from __future__ import annotations

import os

try:
    import opencode_ai
    from opencode_ai import Opencode
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: opencode_ai. Install with 'pip install --pre opencode-ai'"
    ) from exc


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


def main() -> None:
    provider_id = os.getenv("OPENCODE_PROVIDER_ID", "anthropic")
    model_id = os.getenv("OPENCODE_MODEL_ID", "claude-3-5-sonnet-20241022")
    base_url = os.getenv("OPENCODE_BASE_URL")

    client = Opencode(base_url=base_url) if base_url else Opencode()
    try:
        session = client.session.create(extra_body={})
    except opencode_ai.APIConnectionError as exc:
        hint = "Set OPENCODE_BASE_URL to your running OpenCode API (e.g. http://localhost:8083)."
        raise SystemExit(f"Connection error. {hint}") from exc
    except opencode_ai.APIStatusError as exc:
        hint = "The API rejected session creation. Ensure your OpenCode server supports POST /session."
        raise SystemExit(f"Request error. {hint}") from exc
    print(f"Started session: {session.id}")
    print("Type messages and press enter. Type 'exit' to quit.")

    while True:
        try:
            user_text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not user_text:
            continue
        if user_text.lower() in {"exit", "quit"}:
            print("Goodbye.")
            break

        try:
            assistant = client.session.chat(
                session.id,
                provider_id=provider_id,
                model_id=model_id,
                parts=[{"type": "text", "text": user_text}],
            )
        except opencode_ai.APIError as exc:
            print(f"error: {exc}")
            continue

        messages = client.session.messages(session.id)
        last = messages[-1] if messages else None
        reply = ""
        if last is not None:
            reply = _extract_text(getattr(last, "parts", None))
        if not reply:
            reply = "(no assistant reply)"
        print(f"assistant> {reply}")


if __name__ == "__main__":
    main()
