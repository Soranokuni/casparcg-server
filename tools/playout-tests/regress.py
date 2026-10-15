"""Regression checks: untagged (stock) usage is unchanged, the new OSC paths, and a real CG ADD of
PlayOut's advisory template run as a deferred on-air action."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402
from harness import Amcp, OscWatch, check, expect  # noqa: E402

P = "/channel/1/stage/layer"


def main():
    osc = OscWatch()
    osc.start()
    amcp = Amcp()

    # Stock usage: no tags, no new OSC paths.
    expect(amcp, "PLAY 1-20 clipa LOOP", 202, "untagged PLAY")
    expect(amcp, "LOADBG 1-20 clipb AUTO", 202, "untagged LOADBG AUTO")
    time.sleep(0.6)
    with osc.lock:
        layer20 = [a for a in osc.latest if a.startswith(f"{P}/20/")]
    check("untagged layer reports file/path as before", f"{P}/20/foreground/file/path" in layer20)
    check("untagged layer has no tag, queue, fallback or event paths",
          not any(k in a for a in layer20 for k in ("/tag", "/queue/", "/fallback/", "/event/")), str(layer20))
    expect(amcp, "CLEAR 1-20", 202, "CLEAR 1-20")

    # The new OSC paths.
    expect(amcp, "FALLBACK 1-10 slate LOOP TAG fb", 202, "FALLBACK")
    expect(amcp, "PLAY 1-10 clipa LENGTH 100 TAG r1", 202, "PLAY r1")
    expect(amcp, "QUEUE 1-10 clipb LENGTH 60 AUTO TAG r2", 202, "QUEUE r2")
    expect(amcp, "QUEUE 1-10 clipc LENGTH 50 AUTO TAG r3", 202, "QUEUE r3")
    expect(amcp, 'ONAIR 1-10 r2 CG 1-32 ADD 1 "playout/advisory" 1 "{}"', 202, "ONAIR r2 CG ADD advisory")
    time.sleep(0.4)
    f0 = (osc.get("/channel/1/stage/frame") or [0])[0]
    time.sleep(0.2)
    f1 = (osc.get("/channel/1/stage/frame") or [0])[0]
    check("/channel/1/stage/frame advances", f1 > f0 > 0, f"{f0} -> {f1}")
    check("foreground/tag", osc.get(f"{P}/10/foreground/tag") == ["r1"], str(osc.get(f"{P}/10/foreground/tag")))
    check("background/tag and auto", osc.get(f"{P}/10/background/tag") == ["r2"] and
          osc.get(f"{P}/10/background/auto") == [True])
    check("queue/tags", osc.get(f"{P}/10/queue/tags") == ["r3"], str(osc.get(f"{P}/10/queue/tags")))
    check("fallback/tag and active", osc.get(f"{P}/10/fallback/tag") == ["fb"] and
          osc.get(f"{P}/10/fallback/active") == [False])
    check("event/seq and event/onair", (osc.get(f"{P}/10/event/seq") or [0])[0] >= 1 and
          (osc.get(f"{P}/10/event/onair") or [None] * 4)[3] == "r1", str(osc.get(f"{P}/10/event/onair")))

    code, lines = amcp.cmd("INFO 1-10")
    check("INFO carries the tags", code == 201 and "<tag>r1</tag>" in lines[1].replace("\r", "").replace("\n", "")
          or "r1" in "".join(lines), lines[0])

    check("r2 goes on air", osc.wait_event(1, 10, "onair", "r2", 5))
    time.sleep(2.0)
    check("the advisory CG ADD ran without a failure event", not osc.has_event(1, 10, "actionfailed", "r2"))
    code, lines = amcp.cmd("INFO 1-32")
    check("layer 32 runs the html producer", code == 201 and "html" in lines[1], lines[1][:200])

    expect(amcp, "FALLBACK 1-10 CLEAR", 202, "FALLBACK CLEAR")
    expect(amcp, "CLEAR 1", 202, "CLEAR 1")
    failed = [r for r in harness.RESULTS if not r[1]]
    print(f"\n{len(harness.RESULTS) - len(failed)}/{len(harness.RESULTS)} checks passed")


if __name__ == "__main__":
    main()
