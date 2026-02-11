from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ampa.conversation_manager import start_conversation


def main() -> None:
    session_id = f"s-wait-{uuid.uuid4().hex[:8]}"
    work_item = os.getenv("AMPA_EXAMPLE_WORK_ITEM", "WL-EXAMPLE")
    prompt = "Approve deploy?"
    meta = start_conversation(session_id, prompt, {"work_item": work_item})

    print("Started waiting-for-input example")
    print(f"Session: {meta.get('session')}")
    print(f"Work item: {meta.get('work_item')}")
    print(f"Pending prompt file: {meta.get('prompt_file')}")
    print("Check Discord/AMPA for the waiting_for_input notification.")


if __name__ == "__main__":
    main()
