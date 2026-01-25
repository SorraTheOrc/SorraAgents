#!/usr/bin/env python3
"""
json_to_human.py

Convert the final JSON object into a human-readable bulleted narrative.

Accepts stdin JSON (the same shape as the skill final JSON) and prints a ~12-bullet summary.
"""

import sys
import json


def main():
    data = json.load(sys.stdin)
    effort = data.get("effort", {})
    risk = data.get("risk", {})
    confidence = data.get("confidence_percent", 0)
    assumptions = data.get("assumptions", [])
    unknowns = data.get("unknowns", [])

    # Map tshirt codes to readable words
    tshirt_map = {
        "XS": "ExtraSmall",
        "S": "Small",
        "M": "Medium",
        "L": "Large",
        "XL": "XL",
    }

    tsh = effort.get("tshirt", "")
    tsh_read = tshirt_map.get(tsh, tsh or "N/A")

    expected = effort.get("expected", 0)
    score = risk.get("score", 0)
    level = risk.get("level", "")
    unknowns_str = "; ".join(unknowns) if unknowns else "none"

    # Print the report matching the requested template (exact simplified table)
    # Blank line after header
    print("# Effort and Risk Report")
    print("")
    # Use expected (PERT) for the hours cell
    print(f"Effort     | {tsh_read:<7} |  {expected:.2f}h")
    # Use /20 as the denominator as agreed
    print(f"Risk       | {level:<7} |  {score}/20")
    print(f"Confidence | {confidence}%     |  unknowns: {unknowns_str}")


if __name__ == "__main__":
    main()
