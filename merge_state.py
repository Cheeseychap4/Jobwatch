#!/usr/bin/env python3
"""Merge another run's state.json into this one.

state.json is generated, never hand-edited, so rebasing one run's copy over
another's can only conflict - and a conflict used to fail the run and throw
the whole pass away, re-seeding every monitor and risking duplicate alerts.
The two copies are merged instead: seen-lists are unions, ours first, and a
monitor is seeded if either copy says it is.

Usage: merge_state.py <theirs.json> <ours.json>   # ours.json is rewritten
"""

import json
import sys

SEEN_CAP = 600
LIST_CAP = 4000


def merge(ours, theirs):
    for name, block in theirs.items():
        mine = ours.get(name)
        if isinstance(block, list):
            # _alerted and _alerted_keys: plain lists of refs.
            extra = [r for r in block if r not in set(mine or [])]
            ours[name] = ((mine or []) + extra)[:LIST_CAP]
        elif isinstance(block, dict) and isinstance(mine, (dict, type(None))):
            mine = mine or {}
            if name == "_zero":
                # Per-monitor "last said it was empty" stamps: keep the later.
                for k, v in block.items():
                    if v > mine.get(k, 0):
                        mine[k] = v
                ours[name] = mine
                continue
            seen = list(mine.get("seen", []))
            known = set(seen)
            seen += [r for r in block.get("seen", []) if r not in known]
            ours[name] = {"seen": seen[:SEEN_CAP],
                          "seeded": bool(mine.get("seeded")
                                         or block.get("seeded"))}
    return ours


def main():
    theirs_path, ours_path = sys.argv[1], sys.argv[2]
    try:
        with open(theirs_path, encoding="utf-8") as f:
            theirs = json.load(f)
    except Exception:
        theirs = {}
    with open(ours_path, encoding="utf-8") as f:
        ours = json.load(f)
    merged = merge(ours, theirs)
    with open(ours_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=1, sort_keys=True)
    print("merged %s monitor(s) from the other run" % len(theirs))


if __name__ == "__main__":
    main()
