"""Prints the decoded timeline of a recording (clip, source frame, badges) for inspection.

Usage: python -I dump.py rec1.mkv [width height] [first last]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402

path = os.path.join(harness.HERE, sys.argv[1])
width, height = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (320, 180)
rows = harness.timeline(path, 50, width, height)
first = int(sys.argv[4]) if len(sys.argv) > 4 else 0
last = int(sys.argv[5]) if len(sys.argv) > 5 else len(rows)
for r in rows[first:last]:
    print(r["i"], r["clip"], r["n"], "TL" if r["tl"] else "", "BR" if r["br"] else "")
