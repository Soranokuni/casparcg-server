"""Checks that the decoder reads the source clips back correctly."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402

for name, tag in (("clipa", "a"), ("clipb", "b"), ("clipc", "c"), ("clipd", "d"), ("slate", "slate")):
    frames, w, h = harness.decode(os.path.join(harness.HERE, "media", name + ".mov"))
    got = [harness.sample(f, w, h)[:2] for _, f in frames]
    if tag == "slate":
        ok = all(g == ("slate", None) for g in got)
    else:
        ok = got == [(tag, k) for k in range(len(frames))]
    print(name, len(frames), "OK" if ok else f"MISMATCH {got[:5]}")
