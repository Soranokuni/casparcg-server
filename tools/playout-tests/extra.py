"""Extra live checks: BEGIN/COMMIT with QUEUE + ONAIR, PLAY drops the queue, decoder stall failover.

The stall is simulated with an HTTP server that sends the first part of a transport stream and
then stops sending without closing, so the decoder runs dry in the middle of the clip.
"""
import http.server
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402
from harness import Amcp, OscWatch, check, expect, json_reply  # noqa: E402

STALL_PORT = 8099
TS = os.path.join(harness.HERE, "media", "stall.ts")


class StallHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = open(TS, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        cut = len(data) * 6 // 10  # about 1.2 s of the 2 s clip
        self.wfile.write(data[:cut])
        self.wfile.flush()
        time.sleep(20)  # stop sending, keep the connection open

    def log_message(self, *args):
        pass


def main():
    if not os.path.exists(TS):
        subprocess.run([harness.FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i",
                        os.path.join(harness.HERE, "media", "clipb.mov"), "-c:v", "copy", "-c:a", "mp2",
                        "-f", "mpegts", TS], check=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", STALL_PORT), StallHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    osc = OscWatch()
    osc.start()
    amcp = Amcp()

    # BEGIN/COMMIT: the ONAIR in the batch finds the item the batch queued.
    amcp.send("BEGIN", "QUEUE 1-10 clipa LENGTH 25 AUTO TAG x1", "ONAIR 1-10 x1 PLAY 1-31 badge_tl", "COMMIT")
    replies = [amcp.read_reply() for _ in range(3)]
    check("BEGIN/COMMIT with QUEUE and ONAIR", sorted(r[1][0] for r in replies) ==
          ["202 COMMIT OK", "202 ONAIR OK", "202 QUEUE OK"], str(replies))
    check("x1 goes on air (empty layer + AUTO)", osc.wait_event(1, 10, "onair", "x1", 3))
    time.sleep(0.2)
    expect(amcp, "QUEUE 1-10 clipb TAG x2", 202, "QUEUE x2 (not AUTO)")
    expect(amcp, "QUEUE 1-10 clipc AUTO TAG x3", 202, "QUEUE x3")
    expect(amcp, "PLAY 1-10 clipd LENGTH 50 TAG x4", 202, "PLAY x4 (a take)")
    info = json_reply(amcp, "QUEUE 1-10 INFO")
    check("PLAY with a clip drops the waiting items", info["waiting"] == [] and info["foreground"]["tag"] == "x4",
          str(info)[:200])
    expect(amcp, "QUEUE 1-10 clipa AUTO TAG x5", 202, "QUEUE x5")
    expect(amcp, "QUEUE 1-10 clipb AUTO TAG x6", 202, "QUEUE x6")
    expect(amcp, "QUEUE 1-10 clipc AUTO TAG x7", 202, "QUEUE x7")
    expect(amcp, "QUEUE 1-10 CLEAR AFTER x5 EXPECT x4", 201, "QUEUE CLEAR AFTER x5")
    info = json_reply(amcp, "QUEUE 1-10 INFO")
    check("CLEAR AFTER keeps x5 only", [w["tag"] for w in info["waiting"]] == ["x5"], str(info["waiting"]))
    expect(amcp, "QUEUE 1-10 CLEAR AFTER nope", 404, "CLEAR AFTER an unknown tag is 404")
    expect(amcp, "QUEUE 1-10 CLEAR", 201, "QUEUE CLEAR")
    expect(amcp, "CLEAR 1-10", 202, "CLEAR 1-10")
    amcp.cmd("CLEAR 1-31")

    # A live input in ONAIR is not opened when the ONAIR arrives, only after the cut. This laptop has
    # no DeckLink card, so the deferred PLAY fails at the cut and reports actionfailed.
    expect(amcp, "PLAY 1-10 clipa LENGTH 25 TAG l1", 202, "PLAY l1")
    expect(amcp, "QUEUE 1-10 clipb LENGTH 25 AUTO TAG l2", 202, "QUEUE l2")
    expect(amcp, "ONAIR 1-10 l2 PLAY 1-20 DECKLINK 1", 202, "ONAIR with a DeckLink input is accepted (deferred)",
           starts="202 ONAIR OK")
    check("l2 goes on air", osc.wait_event(1, 10, "onair", "l2", 4))
    check("the deferred DeckLink PLAY ran at the cut (fails here: no card)",
          osc.wait_event(1, 10, "actionfailed", "l2", 4))
    expect(amcp, "CLEAR 1-10", 202, "CLEAR 1-10 after the live check")

    # Stall: the stream stops in the middle; after 25 frames without a new frame the layer fails
    # over to the fallback.
    expect(amcp, "FALLBACK 1-10 slate LOOP TAG slate STALL 25", 202, "FALLBACK with STALL 25")
    info = json_reply(amcp, "QUEUE 1-10 INFO")
    check("QUEUE INFO shows the stall limit", info["fallback"]["stall"] == 25, str(info["fallback"]))
    t0 = time.time()
    expect(amcp, f'PLAY 1-10 "http://127.0.0.1:{STALL_PORT}/stall.ts" TAG s', 202, "PLAY a stream that will stall")
    check("s goes on air", osc.wait_event(1, 10, "onair", "s", 10))
    check("the stall is reported", osc.wait_event(1, 10, "stalled", "s", 15), f"{time.time() - t0:.1f}s")
    check("the fallback replaces the stalled item", osc.wait_event(1, 10, "onair", "slate", 3))
    events = json_reply(amcp, "EVENTS 1-10")["events"]
    tail = [(e["kind"], e["tag"], e["reason"]) for e in events if e["tag"] in ("s", "slate")]
    check("event order: onair s, stalled s, offair s (stall), onair slate (stall)",
          tail[-4:] == [("onair", "s", "take"), ("stalled", "s", "stall"), ("offair", "s", "stall"),
                        ("onair", "slate", "stall")], str(tail[-4:]))
    stalled = [e for e in events if e["kind"] == "stalled"][-1]
    onair_s = [e for e in events if e["kind"] == "onair" and e["tag"] == "s"][-1]
    check("about 1.2 s of the stream played before the stall", 40 <= stalled["frame"] - onair_s["frame"] <= 110,
          f"{stalled['frame'] - onair_s['frame']} frames")

    expect(amcp, "FALLBACK 1-10 CLEAR", 202, "FALLBACK CLEAR")
    expect(amcp, "FALLBACK 1-10 STALL 0", 202, "FALLBACK STALL 0")
    expect(amcp, "CLEAR 1-10", 202, "CLEAR 1-10")
    server.shutdown()

    failed = [r for r in harness.RESULTS if not r[1]]
    print(f"\n{len(harness.RESULTS) - len(failed)}/{len(harness.RESULTS)} checks passed")


if __name__ == "__main__":
    main()
