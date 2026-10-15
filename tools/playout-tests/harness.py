"""Live test of the PlayOut fork: TAG, QUEUE, ONAIR, FALLBACK, EVENTS and the SEEK/AUTO fix.

Drives the server over AMCP (port 5260), listens to OSC (port 6260), records the channel output
with the FILE consumer and then checks, frame by frame, that every on-air change and every
on-air action happened on the frame the event log says it did.

Usage: run_all.ps1 runs it. By hand: start the server with the casparcg.config that run_all.ps1
writes, then python -I harness.py
"""
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AMCP_PORT = 5260
OSC_PORT = 6260
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


# --- OSC --------------------------------------------------------------------------------------

def _osc_string(data, i):
    end = data.index(b"\0", i)
    return data[i:end].decode("utf-8", "replace"), (end + 4) & ~3


def parse_osc(data):
    if data.startswith(b"#bundle\0"):
        messages, i = [], 16
        while i < len(data):
            size = struct.unpack(">i", data[i:i + 4])[0]
            messages.extend(parse_osc(data[i + 4:i + 4 + size]))
            i += 4 + size
        return messages

    address, i = _osc_string(data, 0)
    tags, i = _osc_string(data, i)
    args = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack(">i", data[i:i + 4])[0]); i += 4
        elif t == "h":
            args.append(struct.unpack(">q", data[i:i + 8])[0]); i += 8
        elif t == "f":
            args.append(struct.unpack(">f", data[i:i + 4])[0]); i += 4
        elif t == "d":
            args.append(struct.unpack(">d", data[i:i + 8])[0]); i += 8
        elif t == "s":
            value, i = _osc_string(data, i); args.append(value)
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        else:
            raise ValueError(f"OSC type {t}")
    return [(address, args)]


EVENT_RE = re.compile(r"/channel/(\d+)/stage/layer/(\d+)/event/(onair|offair|ended|stalled|actionfailed)$")


class OscWatch(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", OSC_PORT))
        self.sock.settimeout(0.2)
        self.lock = threading.Lock()
        self.latest = {}
        self.events = {}
        self.packets = 0
        self.stopped = False

    def run(self):
        while not self.stopped:
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            with self.lock:
                self.packets += 1
                for address, args in parse_osc(data):
                    self.latest[address] = args
                    m = EVENT_RE.match(address)
                    if m:
                        seq, frame, time_ms, tag, reason = args
                        key = (int(m[1]), int(m[2]), seq)
                        self.events.setdefault(key, dict(seq=seq, frame=frame, time=time_ms, kind=m[3],
                                                         tag=tag, reason=reason))

    def get(self, address):
        with self.lock:
            return self.latest.get(address)

    def has_event(self, channel, layer, kind, tag):
        with self.lock:
            return any(k[0] == channel and k[1] == layer and e["kind"] == kind and e["tag"] == tag
                       for k, e in self.events.items())

    def wait_event(self, channel, layer, kind, tag, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.has_event(channel, layer, kind, tag):
                return True
            time.sleep(0.01)
        return False

    def layer_events(self, channel, layer):
        with self.lock:
            return sorted((e for k, e in self.events.items() if k[0] == channel and k[1] == layer),
                          key=lambda e: e["seq"])


# --- AMCP -------------------------------------------------------------------------------------

class Amcp:
    def __init__(self):
        self.sock = socket.create_connection(("127.0.0.1", AMCP_PORT), timeout=20)
        self.buf = b""

    def _line(self):
        while b"\r\n" not in self.buf:
            data = self.sock.recv(65536)
            if not data:
                raise EOFError("AMCP closed")
            self.buf += data
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line.decode("utf-8")

    def read_reply(self):
        first = self._line()
        code = int(first[:3])
        lines = [first]
        if code == 201 or code == 400:
            lines.append(self._line())
        elif code == 200:
            while True:
                line = self._line()
                if line == "":
                    break
                lines.append(line)
        return code, lines

    def send(self, *commands):
        self.sock.sendall("".join(c + "\r\n" for c in commands).encode("utf-8"))

    def cmd(self, command):
        self.send(command)
        return self.read_reply()


def expect(amcp, command, code, name=None, starts=None):
    got, lines = amcp.cmd(command)
    ok = got == code and (starts is None or lines[0].startswith(starts))
    check(name or command, ok, " | ".join(lines)[:160])
    return got, lines


def json_reply(amcp, command):
    code, lines = amcp.cmd(command)
    if code != 201:
        raise RuntimeError(f"{command}: {lines}")
    return json.loads(lines[1])


# --- recording analysis -----------------------------------------------------------------------

CLIPS = {"a": (98, 158), "b": (158, 98), "c": (98, 98), "d": (158, 158)}


def decode(path, width=320, height=180):
    """Raw yuv444p frames with their pts_time. An interlaced channel records one picture per field."""
    proc = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "info", "-i", path, "-vf", "format=yuv444p,showinfo",
                           "-fps_mode", "passthrough", "-an", "-f", "rawvideo", "-"], capture_output=True)
    times = [float(t) for t in re.findall(rb"pts_time:\s*([-\d.]+)", proc.stderr)]
    size = width * height * 3
    frames = [proc.stdout[k * size:(k + 1) * size] for k in range(len(proc.stdout) // size)]
    return list(zip(times, frames)), width, height


def sample(frame, width, height, fields=False):
    # Coordinates are in the 320x180 layout of the test media; the channel may scale it.
    sx, sy = width / 320, height / 180
    plane = width * height

    def box(p, x0, y0, x1, y1):
        xa, xb = int(x0 * sx), max(int(x1 * sx), int(x0 * sx) + 1)
        ya, yb = int(y0 * sy), max(int(y1 * sy), int(y0 * sy) + 1)
        step = 2 if sx > 1.5 else 1
        total = count = 0
        for y in range(ya, yb, step):
            row = p * plane + y * width
            for x in range(xa, xb, step):
                total += frame[row + x]
                count += 1
        return total / count

    ident = (box(0, 220, 30, 280, 50), box(1, 220, 30, 280, 50), box(2, 220, 30, 280, 50))
    n = sum(1 << k for k in range(7) if box(0, 24 + 20 * k, 84, 36 + 20 * k, 96) > 128)
    if ident[0] < 60:
        clip = "black"
    elif abs(ident[1] - 128) < 12 and abs(ident[2] - 128) < 12:
        clip = "slate"
    else:
        clip = min(CLIPS, key=lambda c: (CLIPS[c][0] - ident[1]) ** 2 + (CLIPS[c][1] - ident[2]) ** 2)
        if (CLIPS[clip][0] - ident[1]) ** 2 + (CLIPS[clip][1] - ident[2]) ** 2 > 25 ** 2:
            clip = "?"
    return clip, (n if clip in CLIPS else None), box(0, 8, 8, 32, 32) > 200, box(0, 288, 148, 312, 172) > 200


def timeline(path, rate, width=320, height=180):
    """One entry per recorded picture (per field on an interlaced channel): clip, source frame, badges."""
    frames, width, height = decode(path, width, height)
    rows = []
    for t, frame in frames:
        clip, n, tl, br = sample(frame, width, height)
        rows.append({"i": round(t * rate), "clip": clip, "n": n, "tl": tl, "br": br})
    return rows


def first_index(rows, clip, n, start=0):
    for k in range(start, len(rows)):
        if rows[k]["clip"] == clip and (n is None or rows[k]["n"] == n):
            return k
    return None


# --- scenarios --------------------------------------------------------------------------------

def scenario_progressive(amcp, osc):
    """Channel 1, 50p: queue, on-air actions, fallback, failover to AUTO, SEEK/AUTO fix."""
    rec = os.path.join(HERE, "rec1.mkv").replace("\\", "/")
    expect(amcp, f'ADD 1-700 FILE "{rec}" -codec:v ffv1 -codec:a pcm_s16le', 202, "record channel 1")
    time.sleep(1.0)

    expect(amcp, "FALLBACK 1-10 slate LOOP TAG slate", 202, "FALLBACK with a looping slate")
    expect(amcp, "PLAY 1-10 clipa SEEK 10 LENGTH 50 TAG a", 202, "PLAY with TAG")
    # a is starting (decoder warm-up) or already on air: attached, or run at once.
    expect(amcp, "ONAIR 1-10 a PLAY 1-31 badge_tl", 202, "ONAIR right after a take", starts="202 ONAIR")
    expect(amcp, "QUEUE 1-10 clipb LENGTH 40 AUTO TAG b", 202, "QUEUE b")
    expect(amcp, "ONAIR 1-10 b CLEAR 1-31", 202, "ONAIR b CLEAR 1-31 attached", starts="202 ONAIR OK")
    expect(amcp, "ONAIR 1-10 b PLAY 1-34 badge_br", 202, "ONAIR b PLAY 1-34 attached", starts="202 ONAIR OK")
    expect(amcp, "QUEUE 1-10 clipc SEEK 20 LENGTH 30 AUTO TAG c", 202, "QUEUE c")
    expect(amcp, "ONAIR 1-10 c CLEAR 1-34", 202, "ONAIR c CLEAR 1-34 attached")
    expect(amcp, "ONAIR 1-10 c PLAY 1-31 badge_tl", 202, "ONAIR c PLAY 1-31 attached")
    expect(amcp, "QUEUE 1-10 clipd LENGTH 25 AUTO TAG d EXPECT zzz", 409, "EXPECT mismatch is refused (409)")
    expect(amcp, "QUEUE 1-10 clipd LENGTH 25 AUTO TAG d EXPECT a", 202, "EXPECT match is accepted")
    expect(amcp, "QUEUE 1-10 REMOVE d", 202, "QUEUE REMOVE d")
    expect(amcp, "QUEUE 1-10 REMOVE d", 404, "QUEUE REMOVE of a missing tag is 404")
    expect(amcp, "QUEUE 1-10 clipd LENGTH 25 AUTO TAG d", 202, "QUEUE d again")
    expect(amcp, "ONAIR 1-10 zz PLAY 1-31 badge_tl", 404, "ONAIR for an unknown tag is 404")
    expect(amcp, 'ONAIR 1-10 d CG 1-32 ADD 1 "missing/template" 1', 202, "ONAIR CG is accepted (deferred)")
    expect(amcp, "ONAIR 1-10 d SWAP 1-31 1-34", 403, "ONAIR refuses SWAP")
    expect(amcp, "ONAIR 1-10 d CLEAR 1", 403, "ONAIR refuses CLEAR of a whole channel")
    expect(amcp, "ONAIR 1-10 d MIXER 1-31 FILL", 403, "ONAIR refuses a MIXER query")
    expect(amcp, "PLAY 1-11 clipa TAG bad/tag", 403, "TAG with a bad character is refused")

    info = json_reply(amcp, "QUEUE 1-10 INFO")
    check("QUEUE INFO: a on air, b c d waiting",
          info["foreground"]["tag"] == "a" and [w["tag"] for w in info["waiting"]] == ["b", "c", "d"]
          and all(w["auto"] for w in info["waiting"]) and info["fallback"]["set"] and not info["fallback"]["active"],
          json.dumps(info)[:200])

    check("b goes on air", osc.wait_event(1, 10, "onair", "b", 4))
    expect(amcp, "ONAIR 1-10 b MIXER 1-20 VOLUME 0.5", 202, "ONAIR for the on-air item runs at once",
           starts="202 ONAIR RAN")

    check("fallback goes on air after d", osc.wait_event(1, 10, "onair", "slate", 8))
    check("CG action failure is reported", osc.wait_event(1, 10, "actionfailed", "d", 3))
    info = json_reply(amcp, "QUEUE 1-10 INFO")
    check("QUEUE INFO: fallback active", info["fallback"]["active"] and info["foreground"]["tag"] == "slate",
          json.dumps(info)[:200])

    time.sleep(0.3)
    expect(amcp, "QUEUE 1-10 clipa LENGTH 20 AUTO TAG a2", 202, "QUEUE a2 while the fallback plays")
    check("the fallback yields to an AUTO item", osc.wait_event(1, 10, "onair", "a2", 2))
    check("the fallback returns after a2", osc.wait_event(1, 10, "offair", "a2", 3))

    # Frame-exact take: load the next item, attach its actions, then take it.
    time.sleep(0.3)
    expect(amcp, "LOADBG 1-10 clipd SEEK 30 LENGTH 20 TAG t", 202, "take recipe: LOADBG t")
    expect(amcp, "ONAIR 1-10 t CLEAR 1-31", 202, "take recipe: ONAIR t CLEAR 1-31", starts="202 ONAIR OK")
    expect(amcp, "ONAIR 1-10 t PLAY 1-34 badge_br", 202, "take recipe: ONAIR t PLAY 1-34", starts="202 ONAIR OK")
    expect(amcp, "PLAY 1-10", 202, "take recipe: PLAY 1-10")
    check("t goes on air", osc.wait_event(1, 10, "onair", "t", 2))
    check("t ends and the fallback returns", osc.wait_event(1, 10, "offair", "t", 3))

    # Upstream bug: before the first frame of a SEEKed clip, frame_number() wrapped to ~4e9, so an
    # AUTO background sent in the same breath went on air at once.
    amcp.send("PLAY 1-10 clipb SEEK 50 LENGTH 40 TAG p", "LOADBG 1-10 clipc LENGTH 30 AUTO TAG q")
    replies = [amcp.read_reply(), amcp.read_reply()]
    check("PLAY SEEK + LOADBG AUTO sent together", all(r[0] == 202 for r in replies), str(replies))
    check("q goes on air", osc.wait_event(1, 10, "onair", "q", 4))

    expect(amcp, "FALLBACK 1-10 CLEAR", 202, "FALLBACK CLEAR")
    check("q ends", osc.wait_event(1, 10, "ended", "q", 3))
    time.sleep(0.3)
    expect(amcp, "CLEAR 1-10", 202, "CLEAR 1-10")
    check("CLEAR reports offair q (clear)", osc.wait_event(1, 10, "offair", "q", 2))
    amcp.cmd("CLEAR 1-31")
    amcp.cmd("CLEAR 1-34")
    time.sleep(0.5)
    expect(amcp, "REMOVE 1-700", 202, "stop recording channel 1")

    events = json_reply(amcp, "EVENTS 1-10")
    since = json_reply(amcp, f"EVENTS 1-10 {events['last'] - 2}")
    check("EVENTS since returns only newer events",
          [e["seq"] for e in since["events"]] == [events["last"] - 1, events["last"]])
    osc_seqs = [e["seq"] for e in osc.layer_events(1, 10)]
    amcp_seqs = [e["seq"] for e in events["events"]]
    check("OSC latched events cover the log", set(amcp_seqs) - set(osc_seqs) == set() or len(osc_seqs) > 0,
          f"amcp={len(amcp_seqs)} osc={len(osc_seqs)}")
    return rec, events["events"]


def analyse_progressive(rec, events):
    rows = timeline(rec, 50)
    check("recording has frames", len(rows) > 100, f"{len(rows)} frames")
    on = {}
    for e in events:
        if e["kind"] == "onair":
            on.setdefault(e["tag"], []).append(e["frame"])

    k_a = first_index(rows, "a", 10)
    check("clip a starts at its SEEK point (source frame 10)", k_a is not None)
    if k_a is None:
        return
    # recording index -> channel frame
    offset = on["a"][0] - rows[k_a]["i"]
    by_frame = {r["i"] + offset: r for r in rows}

    def at(frame):
        return by_frame.get(frame)

    expected = [("b", "a", 0, 59), ("c", "b", 20, 39), ("d", "c", 0, 49)]
    for tag, prev, first_n, prev_last_n in expected:
        f = on[tag][0]
        cur, before = at(f), at(f - 1)
        check(f"onair {tag} is the first frame of {tag} (source {first_n})",
              cur and cur["clip"] == tag and cur["n"] == first_n, str(cur))
        check(f"the frame before onair {tag} is the last frame of {prev} (source {prev_last_n})",
              before and before["clip"] == prev and before["n"] == prev_last_n, str(before))

    # Every clip plays every frame once: source index steps by one per output frame.
    for tag, (start, count) in {"a": (10, 50), "b": (0, 40), "c": (20, 30), "d": (0, 25)}.items():
        f = on[tag][0]
        seq = [at(f + k) for k in range(count)]
        ok = all(s and s["clip"] == tag and s["n"] == start + k for k, s in enumerate(seq))
        check(f"{tag}: {count} frames, each source frame once", ok)

    fb, b_on, c_on = on["slate"][0], on["b"][0], on["c"][0]
    check("slate is on air on the frame after d's last frame",
          at(fb) and at(fb)["clip"] == "slate" and at(fb - 1)["clip"] == "d" and at(fb - 1)["n"] == 24,
          f"{at(fb - 1)} -> {at(fb)}")

    # On-air actions: frame exact at the cut.
    check("b's actions: top-left badge gone on b's first frame", at(b_on - 1)["tl"] and not at(b_on)["tl"],
          f"{at(b_on - 1)} -> {at(b_on)}")
    check("b's actions: bottom-right badge on b's first frame", not at(b_on - 1)["br"] and at(b_on)["br"])
    check("c's actions: bottom-right badge gone on c's first frame", at(c_on - 1)["br"] and not at(c_on)["br"])
    check("c's actions: top-left badge on c's first frame", not at(c_on - 1)["tl"] and at(c_on)["tl"])
    a_on = on["a"][0]
    check("take a: onair is a's first visible frame (source 10)", at(a_on)["clip"] == "a" and at(a_on)["n"] == 10
          and at(a_on - 1)["clip"] == "black", f"{at(a_on - 1)} -> {at(a_on)}")
    lag = next((k for k in range(0, 10) if at(a_on + k) and at(a_on + k)["tl"]), None)
    check("ONAIR right after a take: badge within 3 frames of a's first frame", lag is not None and lag <= 3,
          f"lag={lag}")

    a2 = on["a2"][0]
    check("a2 cuts in over the slate at its source frame 0", at(a2)["clip"] == "a" and at(a2)["n"] == 0 and
          at(a2 - 1)["clip"] == "slate", f"{at(a2 - 1)} -> {at(a2)}")

    t_on = on["t"][0]
    check("take recipe: t's first frame is d/30 with its actions applied",
          at(t_on)["clip"] == "d" and at(t_on)["n"] == 30 and at(t_on)["br"] and not at(t_on)["tl"],
          str(at(t_on)))
    check("take recipe: the frame before is the slate with the old badge",
          at(t_on - 1)["clip"] == "slate" and at(t_on - 1)["tl"] and not at(t_on - 1)["br"], str(at(t_on - 1)))
    t_frames = [at(t_on + k) for k in range(20)]
    check("take recipe: t plays 20 frames, each source frame once",
          all(s and s["clip"] == "d" and s["n"] == 30 + k for k, s in enumerate(t_frames)))
    check("take recipe: slate on the frame after t", at(t_on + 20)["clip"] == "slate", str(at(t_on + 20)))

    p, q = on["p"][0], on["q"][0]
    check("SEEK/AUTO fix: q goes on air after p's 40 frames, not at once", q - p == 40, f"q-p={q - p}")
    check("p starts at source frame 50", at(p)["clip"] == "b" and at(p)["n"] == 50, str(at(p)))


def scenario_interlaced(amcp, osc):
    """Channel 2, PAL (576i50): the queue with LENGTH and SEEK in fields."""
    rec = os.path.join(HERE, "rec2.mkv").replace("\\", "/")
    expect(amcp, f'ADD 2-701 FILE "{rec}" -codec:v ffv1 -codec:a pcm_s16le', 202, "record channel 2")
    time.sleep(1.0)
    expect(amcp, "PLAY 2-10 clipa LENGTH 40 TAG a", 202, "interlaced: PLAY a (40 fields)")
    expect(amcp, "QUEUE 2-10 clipb SEEK 10 LENGTH 30 AUTO TAG b", 202, "interlaced: QUEUE b")
    expect(amcp, "QUEUE 2-10 clipc LENGTH 20 AUTO TAG c", 202, "interlaced: QUEUE c")
    check("interlaced: c ends", osc.wait_event(2, 10, "ended", "c", 6))
    time.sleep(0.3)
    expect(amcp, "CLEAR 2-10", 202, "interlaced: CLEAR")
    time.sleep(0.5)
    expect(amcp, "REMOVE 2-701", 202, "stop recording channel 2")
    events = json_reply(amcp, "EVENTS 2-10")
    return rec, events["events"]


def analyse_interlaced(rec, events):
    rows = timeline(rec, 50, 720, 576)
    check("interlaced recording has fields", len(rows) > 100, f"{len(rows)} fields")
    on = {e["tag"]: e["frame"] for e in events if e["kind"] == "onair"}
    k_a = first_index(rows, "a", 0)
    check("interlaced: a found", k_a is not None)
    if k_a is None:
        return
    offset = 2 * on["a"] - rows[k_a]["i"]
    by_field = {r["i"] + offset: r for r in rows}
    for tag, (start, count) in {"a": (0, 40), "b": (10, 30), "c": (0, 20)}.items():
        f = 2 * on[tag]
        seq = [by_field.get(f + k) for k in range(count)]
        ok = all(s and s["clip"] == tag and s["n"] == start + k for k, s in enumerate(seq))
        check(f"interlaced: {tag} starts on its event frame, {count} fields, each source frame once", ok,
              str(seq[:3]))


def scenario_1080i(amcp, osc):
    """Channel 3, 1080i5000 (the broadcast format): queue, on-air actions, fallback and the
    SEEK/AUTO fix. The recorder scales to 320x180 so it keeps up under emulation."""
    rec = os.path.join(HERE, "rec3.mkv").replace("\\", "/")
    expect(amcp, f'ADD 3-702 FILE "{rec}" -codec:v ffv1 -codec:a pcm_s16le -filter:v scale=320:180', 202,
           "1080i: record channel 3")
    time.sleep(1.0)
    expect(amcp, "FALLBACK 3-10 slate LOOP TAG slate", 202, "1080i: FALLBACK")
    expect(amcp, "PLAY 3-10 clipa SEEK 10 LENGTH 40 TAG a", 202, "1080i: PLAY a (40 fields)")
    expect(amcp, "ONAIR 3-10 a PLAY 3-31 badge_tl", 202, "1080i: ONAIR a", starts="202 ONAIR")
    expect(amcp, "QUEUE 3-10 clipb LENGTH 30 AUTO TAG b", 202, "1080i: QUEUE b")
    expect(amcp, "ONAIR 3-10 b CLEAR 3-31", 202, "1080i: ONAIR b CLEAR 3-31")
    expect(amcp, "ONAIR 3-10 b PLAY 3-34 badge_br", 202, "1080i: ONAIR b PLAY 3-34")
    expect(amcp, "QUEUE 3-10 clipc SEEK 20 LENGTH 20 AUTO TAG c", 202, "1080i: QUEUE c")
    expect(amcp, "ONAIR 3-10 c CLEAR 3-34", 202, "1080i: ONAIR c CLEAR 3-34")
    expect(amcp, "ONAIR 3-10 c PLAY 3-31 badge_tl", 202, "1080i: ONAIR c PLAY 3-31")
    check("1080i: fallback after c", osc.wait_event(3, 10, "onair", "slate", 8))
    time.sleep(0.3)
    amcp.send("PLAY 3-10 clipd SEEK 50 LENGTH 40 TAG p", "LOADBG 3-10 clipb LENGTH 20 AUTO TAG q")
    replies = [amcp.read_reply(), amcp.read_reply()]
    check("1080i: PLAY SEEK + LOADBG AUTO sent together", all(r[0] == 202 for r in replies), str(replies))
    check("1080i: q goes on air", osc.wait_event(3, 10, "onair", "q", 4))
    expect(amcp, "FALLBACK 3-10 CLEAR", 202, "1080i: FALLBACK CLEAR")
    check("1080i: q ends", osc.wait_event(3, 10, "ended", "q", 3))
    time.sleep(0.3)
    for layer in (10, 31, 34):
        amcp.cmd(f"CLEAR 3-{layer}")
    time.sleep(0.5)
    expect(amcp, "REMOVE 3-702", 202, "1080i: stop recording")
    return rec, json_reply(amcp, "EVENTS 3-10")["events"]


def analyse_1080i(rec, events):
    rows = timeline(rec, 50)
    check("1080i: recording has a picture per field", len(rows) > 150, f"{len(rows)} fields")
    on = {}
    for e in events:
        if e["kind"] == "onair":
            on.setdefault(e["tag"], []).append(e["frame"])

    k_a = first_index(rows, "a", 10)
    check("1080i: a found at source frame 10", k_a is not None)
    if k_a is None:
        return
    offset = 2 * on["a"][0] - rows[k_a]["i"]
    by_field = {r["i"] + offset: r for r in rows}

    def at(field):
        return by_field.get(field)

    check("1080i: take a: the field before a's first field is black", at(2 * on["a"][0] - 1)["clip"] == "black")
    for tag, (start, count), prev in (("b", (0, 30), ("a", 49)), ("c", (20, 20), ("b", 29))):
        f = 2 * on[tag][0]
        seq = [at(f + k) for k in range(count)]
        check(f"1080i: {tag} starts on field a of its event frame, {count} fields, each source frame once",
              all(s and s["clip"] == tag and s["n"] == start + k for k, s in enumerate(seq)), str(seq[:2]))
        before = at(f - 1)
        check(f"1080i: the field before {tag} is the last field of {prev[0]} (source {prev[1]})",
              before and before["clip"] == prev[0] and before["n"] == prev[1], str(before))

    b, c = 2 * on["b"][0], 2 * on["c"][0]
    check("1080i: b's actions on b's first field", at(b - 1)["tl"] and not at(b)["tl"] and at(b)["br"]
          and not at(b - 1)["br"], f"{at(b - 1)} -> {at(b)}")
    check("1080i: c's actions on c's first field", at(c - 1)["br"] and not at(c)["br"] and at(c)["tl"]
          and not at(c - 1)["tl"], f"{at(c - 1)} -> {at(c)}")
    fb = 2 * on["slate"][0]
    check("1080i: slate on the field after c's last field",
          at(fb)["clip"] == "slate" and at(fb - 1)["clip"] == "c" and at(fb - 1)["n"] == 39,
          f"{at(fb - 1)} -> {at(fb)}")
    p, q = on["p"][0], on["q"][0]
    check("1080i: SEEK/AUTO fix: q on air 20 frames (40 fields) after p", q - p == 20, f"q-p={q - p}")


def main():
    osc = OscWatch()
    osc.start()
    amcp = Amcp()
    code, lines = amcp.cmd("VERSION")
    check("server answers", code == 201, lines[-1])

    rec1, ev1 = scenario_progressive(amcp, osc)
    rec2, ev2 = scenario_interlaced(amcp, osc)
    rec3, ev3 = scenario_1080i(amcp, osc)
    with open(os.path.join(HERE, "events.json"), "w") as f:
        json.dump({"channel1": ev1, "channel2": ev2, "channel3": ev3}, f, indent=1)

    analyse_progressive(rec1, ev1)
    analyse_interlaced(rec2, ev2)
    analyse_1080i(rec3, ev3)
    check("OSC packets received", osc.packets > 50, str(osc.packets))

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name, _, detail in failed:
        print("FAILED:", name, detail)


if __name__ == "__main__":
    main()
