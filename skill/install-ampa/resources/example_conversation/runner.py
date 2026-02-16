#!/usr/bin/env python3
"""Simple example conversation engine: two managers exchange messages.

Produces a newline-delimited JSON transcript with timestamps and session ids.
"""

import argparse
import json
import time
import uuid
from datetime import datetime
from typing import Dict, Any


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


class ConversationManager:
    def __init__(self, name: str, seed: str):
        self.name = name
        self.session_id = f"{name}-{uuid.uuid4().hex[:8]}"
        self.last_message = seed

    def respond(self, incoming: str, turn: int) -> str:
        # deterministic responder: echo with small modification
        return f"{self.name} reply {turn}: to '{incoming}'"


def run(rounds: int, out_path: str, seed_a: str, seed_b: str):
    a = ConversationManager("CM-A", seed_a)
    b = ConversationManager("CM-B", seed_b)

    transcript = []

    # initial message from A using its seed
    current_sender = a
    other = b
    message = a.last_message

    for turn in range(1, rounds * 2 + 1):
        ts = now_iso()
        entry = {
            "timestamp": ts,
            "turn": turn,
            "sender": current_sender.name,
            "session_id": current_sender.session_id,
            "message": message,
        }
        transcript.append(entry)

        # prepare response from other
        response = other.respond(message, turn)

        # rotate
        message = response
        current_sender, other = other, current_sender

    # write newline-delimited JSON
    with open(out_path, "w", encoding="utf-8") as f:
        for e in transcript:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"Wrote {len(transcript)} messages to {out_path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument("--out", default="transcript.jsonl")
    p.add_argument("--seed-a", default="Hello from A")
    p.add_argument("--seed-b", default="Hello from B")
    args = p.parse_args()

    run(args.rounds, args.out, args.seed_a, args.seed_b)


if __name__ == "__main__":
    main()
