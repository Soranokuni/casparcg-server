"""Live checks for the fallback round of 2026-10-16: absolute fallback paths, no fallback flash on a
take into an empty layer (F2), and FALLBACK LIST (fillers).

Runs on channel 1 (50p). The filler clips are 20-frame cuts of the harness clips, so the recording
shows which filler is on air (chroma) and which of its frames (the binary blocks).
"""
import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402
from harness import Amcp, OscWatch, check, expect, json_reply  # noqa: E402

MEDIA = os.path.join(harness.HERE, "media")
OUTSIDE = os.path.join(harness.HERE, "outside")  # not under the media path
FILLER_FRAMES = 20
# A broken file is found out within 1 s; the filler before it must play longer to hide that.
BROKEN_FRAMES = 100


def cut(src, dst, frames):
    # Re-encoded as make_media.py does: a stream copy of 0.4 s keeps a 21st frame.
    subprocess.run([harness.FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", os.path.join(MEDIA, src),
                    "-frames:v", str(frames), "-t", str(frames / 50), "-c:v", "mpeg2video", "-g", "1", "-q:v", "1",
                    "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-ac", "2", dst], check=True)


def make_media():
    os.makedirs(OUTSIDE, exist_ok=True)
    cut("slate.mov", os.path.join(OUTSIDE, "slate_abs.mov"), 50)

    folders = {"fillers": ["clipa", "clipb", "clipc", "clipd"], "fillers_broken": ["clipb", None, "clipc"],
               "fillers_dead": [None, None], "fillers_empty": []}
    for folder, clips in folders.items():
        path = os.path.join(MEDIA, folder)
        os.makedirs(path, exist_ok=True)
        for old in glob.glob(os.path.join(path, "*")):
            os.remove(old)
        for k, clip in enumerate(clips, start=1):
            dst = os.path.join(path, f"f{k}.mov")
            if clip:
                cut(f"{clip}.mov", dst, BROKEN_FRAMES if folder == "fillers_broken" else FILLER_FRAMES)
            else:
                with open(dst, "wb") as f:
                    f.write(os.urandom(4096))  # not a movie
    # Not a video file: an "empty" folder still has something in it.
    with open(os.path.join(MEDIA, "fillers_empty", "readme.txt"), "w") as f:
        f.write("no videos here\n")


def server_log_lines(pattern):
    logs = sorted(glob.glob(os.path.join(harness.HERE, "log", "*.log")), key=os.path.getmtime)
    if not logs:
        return 0
    with open(logs[-1], encoding="utf-8", errors="replace") as f:
        return sum(1 for line in f if pattern in line)


def events_after(amcp, seq):
    return json_reply(amcp, f"EVENTS 1-10 {seq}")["events"]


def last_seq(amcp):
    return json_reply(amcp, "EVENTS 1-10")["last"]


def wait_event(amcp, seq, kind, tag, timeout):
    """An event after seq, from the layer's log (OSC re-sends old events, so it cannot tell new from old)."""
    return wait_for(lambda: next((e for e in events_after(amcp, seq) if e["kind"] == kind and e["tag"] == tag), None),
                    timeout)


def wait_for(fn, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    return None


def scenario_absolute(amcp, osc):
    """FK-1: the fallback clip may be an absolute path outside the media folder."""
    abs_clip = os.path.join(OUTSIDE, "slate_abs").replace("\\", "/")
    expect(amcp, f'FALLBACK 1-10 "{abs_clip}.mov" LOOP TAG slate', 202, "FALLBACK with an absolute path (D:/…/x.mov)")
    expect(amcp, f'FALLBACK 1-10 "{abs_clip}" LOOP TAG slate', 202, "FALLBACK with an absolute path, no extension")
    expect(amcp, f'FALLBACK 1-10 "{abs_clip}_missing" LOOP TAG slate', 404, "FALLBACK with a missing absolute path is 404")
    info = json_reply(amcp, "QUEUE 1-10 INFO")["fallback"]
    check("QUEUE INFO: absolute clip fallback is set, mode clip, 1 file",
          info["set"] and info["mode"] == "clip" and info["files"] == 1 and info["current"] is None, str(info))

    seq = last_seq(amcp)
    expect(amcp, "PLAY 1-10 clipa LENGTH 20 TAG k1", 202, "PLAY k1 before the absolute fallback")
    check("the absolute fallback goes on air after k1", wait_event(amcp, seq, "onair", "slate", 4))
    events = events_after(amcp, seq)
    on = [e for e in events if e["kind"] == "onair" and e["tag"] == "slate"]
    check("onair slate (ended) after k1", on and on[0]["reason"] == "ended", str(on[:1]))
    info = json_reply(amcp, "QUEUE 1-10 INFO")["fallback"]
    check("QUEUE INFO: current is the clip as given", info["active"] and info["current"] == abs_clip, str(info))
    expect(amcp, "CLEAR 1-10", 202, "CLEAR after the absolute fallback")


def scenario_f2(amcp, osc):
    """F2: a take into an empty layer never shows the fallback, not even for a frame."""
    expect(amcp, "FALLBACK 1-10 slate LOOP TAG slate", 202, "F2: FALLBACK set")
    expect(amcp, "CLEAR 1-10", 202, "F2: empty layer")
    time.sleep(0.1)  # the CLEAR's offair event is stamped on the next tick
    replaced = server_log_lines("empty replaced by fallback")
    seq = last_seq(amcp)

    expect(amcp, "LOADBG 1-10 clipb LENGTH 30 TAG f2a", 202, "F2: LOADBG into the empty layer")
    time.sleep(0.3)  # several ticks with an empty foreground and a background
    expect(amcp, "PLAY 1-10", 202, "F2: PLAY 1-10")
    check("F2: f2a goes on air", wait_event(amcp, seq, "onair", "f2a", 3))
    events = events_after(amcp, seq)
    first = [(e["kind"], e["tag"], e["reason"]) for e in events][:2]
    check("F2: no onair fallback before the take", not any(e["tag"] == "slate" for e in events)
          and first and first[0] == ("onair", "f2a", "take"), str(first))

    # A dry queue after a real item still falls back on the next frame.
    check("F2: the fallback still follows a real item that ends", wait_event(amcp, seq, "onair", "slate", 3))
    events = events_after(amcp, seq)
    ended = next((e for e in events if e["kind"] == "ended" and e["tag"] == "f2a"), None)
    on = next((e for e in events if e["kind"] == "onair" and e["tag"] == "slate"), None)
    check("F2: onair slate (ended) on the tick f2a ended",
          ended and on and on["frame"] == ended["frame"] and on["reason"] == "ended", f"{ended} {on}")

    # QUEUE without AUTO and STOP leave an empty layer empty.
    expect(amcp, "CLEAR 1-10", 202, "F2: CLEAR")
    time.sleep(0.1)
    seq = last_seq(amcp)
    expect(amcp, "QUEUE 1-10 clipc LENGTH 20 TAG f2b", 202, "F2: QUEUE (not AUTO) into the empty layer")
    time.sleep(0.3)
    expect(amcp, "PLAY 1-10", 202, "F2: PLAY the queued item")
    check("F2: f2b goes on air", wait_event(amcp, seq, "onair", "f2b", 3))
    time.sleep(0.1)
    expect(amcp, "STOP 1-10", 202, "F2: STOP while f2b plays")
    time.sleep(0.3)
    events = events_after(amcp, seq)
    check("F2: no fallback after QUEUE into an empty layer or after STOP",
          not any(e["tag"] == "slate" for e in events), str([(e["kind"], e["tag"], e["reason"]) for e in events]))
    check("F2: no 'empty replaced by fallback' in the server log",
          server_log_lines("empty replaced by fallback") == replaced)
    expect(amcp, "CLEAR 1-10", 202, "F2: CLEAR at the end")


def scenario_list(amcp, osc):
    """FALLBACK LIST: rotation, gapless cuts, AUTO and PLAY replace a filler, QUEUE INFO, events."""
    rec = os.path.join(harness.HERE, "rec4.mkv").replace("\\", "/")
    expect(amcp, f'ADD 1-702 FILE "{rec}" -codec:v ffv1 -codec:a pcm_s16le', 202, "record channel 1 (fillers)")
    time.sleep(1.0)

    expect(amcp, 'FALLBACK 1-10 LIST "fillers_empty"', 404, "FALLBACK LIST of a folder without videos is 404")
    expect(amcp, 'FALLBACK 1-10 LIST "no_such_folder"', 404, "FALLBACK LIST of a missing folder is 404")
    expect(amcp, 'FALLBACK 1-10 LIST "fillers" TAG fill', 202, "FALLBACK LIST fillers (in order)")
    info = json_reply(amcp, "QUEUE 1-10 INFO")["fallback"]
    check("QUEUE INFO: mode list, 4 files, nothing current",
          info["set"] and info["mode"] == "list" and info["files"] == 4 and info["current"] is None
          and info["tag"] == "fill", str(info))

    seq = last_seq(amcp)
    expect(amcp, "PLAY 1-10 clipa SEEK 50 LENGTH 20 TAG r1", 202, "PLAY r1 (the queue then runs dry)")
    check("fillers rotate: fill:6 goes on air", wait_event(amcp, seq, "onair", "fill:6", 8))
    info = json_reply(amcp, "QUEUE 1-10 INFO")
    check("QUEUE INFO while a filler plays: active, current file, tagged fill:n",
          info["fallback"]["active"] and info["fallback"]["current"] in ("f1.mov", "f2.mov", "f3.mov", "f4.mov")
          and info["foreground"]["tag"].startswith("fill:"), str(info["fallback"]) + " " + str(info["foreground"]))

    frame0 = json_reply(amcp, "QUEUE 1-10 INFO")["frame"]
    expect(amcp, "QUEUE 1-10 clipd SEEK 60 LENGTH 20 AUTO TAG r2", 202, "QUEUE r2 AUTO while fillers play")
    check("r2 replaces the filler", wait_event(amcp, seq, "onair", "r2", 3))
    check("fillers come back after r2", wait_for(
        lambda: any(e["kind"] == "onair" and e["tag"].startswith("fill:") and e["frame"] > frame0
                    and e["reason"] == "ended" for e in events_after(amcp, seq)), 3))
    time.sleep(0.5)
    expect(amcp, "PLAY 1-10 clipc SEEK 70 LENGTH 20 TAG r3", 202, "PLAY r3 (a take) while fillers play")
    check("r3 replaces the filler", wait_event(amcp, seq, "onair", "r3", 3))
    expect(amcp, "FALLBACK 1-10 CLEAR", 202, "FALLBACK CLEAR (the list)")
    check("r3 ends", wait_event(amcp, seq, "ended", "r3", 3))
    time.sleep(0.3)
    expect(amcp, "CLEAR 1-10", 202, "CLEAR after the fillers")
    time.sleep(0.5)
    expect(amcp, "REMOVE 1-702", 202, "stop recording channel 1 (fillers)")

    events = events_after(amcp, seq)
    onair = [e for e in events if e["kind"] == "onair"]
    fills = [e for e in onair if e["tag"].startswith("fill:")]
    files = [e.get("file") for e in fills]
    check("every filler onair carries its file", fills and all(files), str(files))
    first_run = files[:6]
    check("rotation in name order: f1 f2 f3 f4 f1 f2",
          first_run == ["f1.mov", "f2.mov", "f3.mov", "f4.mov", "f1.mov", "f2.mov"], str(first_run))
    check("filler tags count up: fill:1, fill:2, ...",
          [e["tag"] for e in fills] == [f"fill:{k}" for k in range(1, len(fills) + 1)], str([e["tag"] for e in fills]))
    check("the first filler comes on air with reason ended", fills[0]["reason"] == "ended", str(fills[0]))

    # Between fillers: ended fill:n, offair fill:n (auto), onair fill:n+1 (auto), all on one tick.
    ok = True
    detail = ""
    for prev, nxt in zip(fills[:5], fills[1:6]):
        tail = [(e["kind"], e["tag"], e["reason"], e["frame"]) for e in events
                if e["tag"] in (prev["tag"], nxt["tag"]) and prev["frame"] < e["frame"] <= nxt["frame"]]
        want = [("ended", prev["tag"], "ended", nxt["frame"]), ("offair", prev["tag"], "auto", nxt["frame"]),
                ("onair", nxt["tag"], "auto", nxt["frame"])]
        if tail != want or nxt["frame"] - prev["frame"] != FILLER_FRAMES:
            ok, detail = False, f"{tail} gap={nxt['frame'] - prev['frame']}"
            break
    check("between fillers: ended, offair (auto), onair (auto) on one tick, 20 frames apart", ok, detail)
    ended_fill = next(e for e in events if e["kind"] == "ended" and e["tag"] == "fill:1")
    check("events of a filler carry the file (ended)", ended_fill.get("file") == "f1.mov", str(ended_fill))

    r2 = next(e for e in onair if e["tag"] == "r2")
    check("AUTO item replaces the filler within a few frames", r2["reason"] == "auto" and r2["frame"] - frame0 <= 5,
          f"{r2['frame'] - frame0} frames after the QUEUE")
    r3 = next(e for e in onair if e["tag"] == "r3")
    check("PLAY replaces the filler (take)", r3["reason"] == "take", str(r3))

    osc_fill = [e for e in osc.layer_events(1, 10) if e["tag"].startswith("fill:") and e["kind"] == "onair"]
    check("OSC events of fillers carry the file as a sixth argument",
          osc_fill and all(e["file"] and e["file"].endswith(".mov") for e in osc_fill), str(osc_fill[:2]))
    return rec, events


def analyse_list(rec, events):
    rows = harness.timeline(rec, 50)
    check("filler recording has frames", len(rows) > 100, f"{len(rows)} frames")
    on = {e["tag"]: e for e in events if e["kind"] == "onair"}
    k_r1 = harness.first_index(rows, "a", 50)
    check("r1 starts at source frame 50", k_r1 is not None)
    if k_r1 is None:
        return
    offset = on["r1"]["frame"] - rows[k_r1]["i"]
    by_frame = {r["i"] + offset: r for r in rows}
    chroma = {"f1.mov": "a", "f2.mov": "b", "f3.mov": "c", "f4.mov": "d"}

    fills = [e for e in events if e["kind"] == "onair" and e["tag"].startswith("fill:")]
    first = fills[0]
    before = by_frame.get(first["frame"] - 1)
    check("the first filler follows r1's last frame (a/69) with no gap",
          before and before["clip"] == "a" and before["n"] == 69, str(before))

    # Every filler before r2 plays its 20 frames once, back to back.
    ok, detail = True, ""
    for e in fills[:5]:  # fill:6 is cut short by r2
        seq = [by_frame.get(e["frame"] + k) for k in range(FILLER_FRAMES)]
        want = chroma[e["file"]]
        if not all(s and s["clip"] == want and s["n"] == k for k, s in enumerate(seq)):
            ok, detail = False, f"{e['tag']} {e['file']}: {[(s['clip'], s['n']) if s else None for s in seq]}"
            break
    check("gapless: each filler shows its 20 frames once, the next starts on the following frame", ok, detail)

    r2 = on["r2"]
    cur, prev = by_frame.get(r2["frame"]), by_frame.get(r2["frame"] - 1)
    check("r2 cuts in over a filler at its source frame 60",
          cur and cur["clip"] == "d" and cur["n"] == 60 and prev and prev["clip"] in "abcd", f"{prev} -> {cur}")


def scenario_broken(amcp, osc):
    """A filler that cannot be opened is skipped with actionfailed; with none left the layer holds."""
    seq = last_seq(amcp)
    expect(amcp, 'FALLBACK 1-10 LIST "fillers_broken" TAG brk', 202, "FALLBACK LIST with a broken file")
    expect(amcp, "PLAY 1-10 clipa LENGTH 10 TAG r4", 202, "PLAY r4")
    check("broken list: brk:3 goes on air", wait_event(amcp, seq, "onair", "brk:3", 8))
    events = events_after(amcp, seq)
    files = [e.get("file") for e in events if e["kind"] == "onair" and e["tag"].startswith("brk:")]
    check("broken file skipped: f1 f3 f1", files[:3] == ["f1.mov", "f3.mov", "f1.mov"], str(files))
    failed = [e for e in events if e["kind"] == "actionfailed" and e["tag"] == "brk"]
    check("broken file reported as actionfailed with its file",
          failed and all(e.get("file") == "f2.mov" and e["reason"].startswith("FALLBACK LIST") for e in failed),
          str(failed[:1]))
    gaps = [b["frame"] - a["frame"] for a, b in zip(
        [e for e in events if e["kind"] == "onair" and e["tag"].startswith("brk:")][:3],
        [e for e in events if e["kind"] == "onair" and e["tag"].startswith("brk:")][1:3])]
    check("no gap where the broken file was skipped", gaps == [BROKEN_FRAMES, BROKEN_FRAMES], str(gaps))
    expect(amcp, "CLEAR 1-10", 202, "CLEAR after the broken list")

    seq = last_seq(amcp)
    expect(amcp, "FALLBACK 1-10 CLEAR", 202, "FALLBACK CLEAR before the dead list (no slate)")
    expect(amcp, 'FALLBACK 1-10 LIST "fillers_dead" TAG dead', 202, "FALLBACK LIST where every file is broken")
    expect(amcp, "PLAY 1-10 clipb LENGTH 10 TAG r5", 202, "PLAY r5")
    check("no filler opens: stalled (fallback)", wait_event(amcp, seq, "stalled", "dead", 8))
    events = events_after(amcp, seq)
    stalled = [e for e in events if e["kind"] == "stalled"]
    check("stalled reason is fallback", stalled and stalled[0]["reason"] == "fallback", str(stalled[:1]))
    info = json_reply(amcp, "QUEUE 1-10 INFO")
    check("the layer holds r5", info["foreground"]["tag"] == "r5" and not info["fallback"]["active"],
          str(info["foreground"]))
    expect(amcp, "CLEAR 1-10", 202, "CLEAR after the dead list")

    # F-e: a clip set before the list stays as the slate and airs when every filler fails.
    seq = last_seq(amcp)
    expect(amcp, "FALLBACK 1-10 slate LOOP TAG dead", 202, "FALLBACK <clip> before the dead list (the slate)")
    expect(amcp, 'FALLBACK 1-10 LIST "fillers_dead" TAG dead', 202, "FALLBACK LIST keeps the clip as the slate")
    info = json_reply(amcp, "QUEUE 1-10 INFO")["fallback"]
    check("QUEUE INFO: mode list with the slate", info["mode"] == "list" and info["slate"] == "slate", str(info))
    expect(amcp, "PLAY 1-10 clipb LENGTH 10 TAG r5b", 202, "PLAY r5b")
    check("no filler opens: stalled (fallback slate)", wait_event(amcp, seq, "stalled", "dead", 8))
    events = events_after(amcp, seq)
    stalled = [e for e in events if e["kind"] == "stalled"]
    check("stalled reason is fallback slate", stalled and stalled[0]["reason"] == "fallback slate", str(stalled[:1]))
    check("the slate goes on air", wait_event(amcp, seq, "onair", "dead", 3))
    info = json_reply(amcp, "QUEUE 1-10 INFO")
    check("QUEUE INFO: the slate is current", info["fallback"]["active"] and info["fallback"]["current"] == "slate",
          str(info["fallback"]))
    expect(amcp, "PLAY 1-10 clipa LENGTH 10 TAG r5c", 202, "PLAY replaces the slate")
    check("r5c replaces the slate", wait_event(amcp, seq, "onair", "r5c", 3))
    expect(amcp, "CLEAR 1-10", 202, "CLEAR after the slate")
    expect(amcp, "FALLBACK 1-10 CLEAR", 202, "FALLBACK CLEAR removes list and slate")
    info = json_reply(amcp, "QUEUE 1-10 INFO")["fallback"]
    check("QUEUE INFO after FALLBACK CLEAR: no slate", not info["set"] and info.get("slate") is None, str(info))


def scenario_shuffle(amcp, osc):
    """SHUFFLE with NOREPEAT: no file comes back within n picks; a clip replaces LIST, LIST keeps it."""
    seq = last_seq(amcp)
    expect(amcp, 'FALLBACK 1-10 LIST "fillers" SHUFFLE NOREPEAT 2 TAG shf', 202, "FALLBACK LIST SHUFFLE NOREPEAT 2")
    expect(amcp, "PLAY 1-10 clipa LENGTH 10 TAG r6", 202, "PLAY r6")
    check("shuffle: shf:12 goes on air", wait_event(amcp, seq, "onair", "shf:12", 10))
    events = events_after(amcp, seq)
    files = [e["file"] for e in events if e["kind"] == "onair" and e["tag"].startswith("shf:")]
    repeats = [k for k in range(len(files)) if files[k] in files[max(0, k - 2):k]]
    check("NOREPEAT 2: no file within 2 picks of itself", len(files) >= 12 and not repeats, str(files))
    check("shuffle uses every file", set(files) == {"f1.mov", "f2.mov", "f3.mov", "f4.mov"}, str(set(files)))

    seq = last_seq(amcp)
    expect(amcp, "FALLBACK 1-10 slate LOOP TAG slate", 202, "FALLBACK <clip> replaces the list")
    info = json_reply(amcp, "QUEUE 1-10 INFO")["fallback"]
    check("QUEUE INFO: mode clip after FALLBACK <clip>", info["mode"] == "clip" and info["files"] == 1, str(info))
    check("the clip follows the running filler", wait_event(amcp, seq, "onair", "slate", 3))
    expect(amcp, 'FALLBACK 1-10 LIST "fillers" TAG fill2', 202, "FALLBACK LIST keeps the clip as the slate")
    info = json_reply(amcp, "QUEUE 1-10 INFO")["fallback"]
    check("QUEUE INFO: mode list after FALLBACK LIST, clip kept as slate",
          info["mode"] == "list" and info["files"] == 4 and info["slate"] == "slate", str(info))
    expect(amcp, "CLEAR 1-10", 202, "CLEAR after shuffle")
    expect(amcp, "FALLBACK 1-10 CLEAR", 202, "FALLBACK CLEAR at the end")
    info = json_reply(amcp, "QUEUE 1-10 INFO")["fallback"]
    check("QUEUE INFO after FALLBACK CLEAR: not set", not info["set"] and info["files"] == 0, str(info))


def main():
    make_media()
    osc = OscWatch()
    osc.start()
    amcp = Amcp()
    amcp.cmd("CLEAR 1")

    scenario_absolute(amcp, osc)
    scenario_f2(amcp, osc)
    rec, events = scenario_list(amcp, osc)
    scenario_broken(amcp, osc)
    scenario_shuffle(amcp, osc)
    analyse_list(rec, events)

    failed = [r for r in harness.RESULTS if not r[1]]
    print(f"\n{len(harness.RESULTS) - len(failed)}/{len(harness.RESULTS)} checks passed")


if __name__ == "__main__":
    main()
