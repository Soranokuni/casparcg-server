# CasparCG Server — PlayOut fork

This fork of CasparCG Server 2.5.1 gives the playout engine the parts that PlayOut had to infer from
outside: which item is on air, a queue of next items, actions that run on the exact frame an item
goes on air, an emergency fallback, and a reliable event log. Without these, PlayOut has to guess
the engine state from OSC file paths and positions. Everything else stays stock 2.5.1.

- Repository: <https://github.com/Soranokuni/casparcg-server>, a GitHub fork of CasparCG/server,
  branch `playout/2.5.1`. Nothing goes to the official repository: in the local clone, remote
  `origin` (CasparCG/server) is fetch-only (push URL `no-push`), and remote `fork` is this repository.
- Base: `v2.5.1-stable` (`ec38d944`), branch `playout/2.5.1`.
- `43a7ffee` — build with Boost 1.83 (needed to run on the Windows-on-ARM development laptop).
- This change set: TAG, QUEUE, ONAIR, FALLBACK/STALL, EVENTS, a fix for an upstream bug (§7) and a
  console input fix (§6).
- `520596b2` — merge of `origin/v2.5.x`: upstream's fixes after v2.5.1.
- Target: x64 broadcast PCs. Development and build: Windows 11 on ARM64 (see §2).

Contents

1. [What changed, in short](#1-what-changed-in-short)
2. [Build, run and deploy](#2-build-run-and-deploy)
3. [Concepts](#3-concepts)
4. [AMCP reference](#4-amcp-reference)
5. [OSC reference](#5-osc-reference)
6. [Behaviour changes against stock 2.5.1](#6-behaviour-changes-against-stock-251)
7. [Upstream bug fixed: SEEK + AUTO skipped the clip](#7-upstream-bug-fixed-seek--auto-skipped-the-clip)
8. [How PlayOut uses the fork](#8-how-playout-uses-the-fork)
9. [Tests](#9-tests)
10. [Limits and open points](#10-limits-and-open-points)
11. [Source map and commits](#11-source-map-and-commits)

---

## 1. What changed, in short

| Feature | What it gives PlayOut | Commands |
|---|---|---|
| **Item tags** | Every item carries PlayOut's row id. The engine reports which tag is on air. | `TAG <id>` on `PLAY`, `LOAD`, `LOADBG`, `QUEUE` |
| **Event log** | `onair`, `offair`, `ended`, `stalled`, `actionfailed` per item, stamped with the channel frame number and the wall clock. | `EVENTS`, OSC `…/event/*` |
| **Wait queue** | The engine holds the next N items. It plays them back to back itself, so the channel keeps running when PlayOut is slow, busy or dead. | `QUEUE`, `QUEUE CLEAR`, `QUEUE REMOVE`, `QUEUE INFO` |
| **On-air actions** | Commands attached to an item run when the item goes on air. Stage commands (rating, TP, clears, mixer) show on the item's first frame. | `ONAIR <tag> <command>` |
| **Fallback and stall failover** | When the queue runs dry, a decoder stalls or a producer fails, the layer switches to the next item or to a warm emergency clip, instead of freezing or going black. | `FALLBACK`, `FALLBACK CLEAR`, `FALLBACK STALL` |
| **Optimistic concurrency** | Queue edits carry `EXPECT <tag on air>`. The engine refuses a stale edit with `409`, so a rundown edit can never race the cut. | `EXPECT <tag>` |
| **Bug fix** | `PLAY … SEEK n` followed by `LOADBG … AUTO` no longer skips the clip (§7). | — |

Untagged use is unchanged: no new OSC addresses, no events, same replies (§6).

---

## 2. Build, run and deploy

### Build on the Windows-on-ARM laptop (x64 target)

The build machine is ARM64. The broadcast PCs are x64. Build x64 with the ARM64-hosted MSVC tools.
A full build takes about 2 minutes. Dependencies (about 280 MB) are cached in `build\external`.

```powershell
$env:CMAKE_POLICY_VERSION_MINIMUM = '3.5'   # CMake 4.x and the old ExternalProjects
cmake -S src -B build -G "Visual Studio 17 2022" -A x64 -T host=ARM64 `
  "-DCMAKE_GENERATOR_INSTANCE=C:/Program Files/Microsoft Visual Studio/2022/Community" `
  "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"
cmake --build build --config Release --parallel 12
```

- Quote every `-D...=x.y` argument. PowerShell splits an unquoted argument at the dot.
- On an x64 build machine, remove `-T host=ARM64` (and the generator instance if Visual Studio is in
  its default location).
- The runnable output is the whole folder `build\shell\Release`.

### Run on the laptop

- The x64 server runs under Prism emulation. OpenGL comes from Microsoft's "OpenCL, OpenGL and Vulkan
  Compatibility Pack". Do not copy Mesa DLLs next to the executable.
- Stock 2.5.0/2.5.1 builds use Boost 1.74 and crash on the first drawn frame on this laptop. This
  fork uses Boost 1.83 (`43a7ffee`), as upstream master does.
- There is no DeckLink card on the laptop. DeckLink input and output must be tested on the broadcast PC.

### Deploy to a broadcast PC

1. Copy `build\shell\Release` to the PC (for example next to the current server).
2. Keep the PC's `casparcg.config`. The fork reads the same configuration file.
3. Install the latest Microsoft Visual C++ x64 runtime.
4. CPU: AVX2 is recommended for 2.5 and required from 2.6.
5. Point PlayOut's "CasparCG executable" setting at the new `casparcg.exe`.

---

## 3. Concepts

### 3.1 Items and tags

An *item* is one producer on one layer: a clip, an image, a live input, a template. A *tag* is the
item's identity, given by the client. PlayOut uses the rundown row id (`RundownItem.id`, a uuid).

- Characters: `A-Z a-z 0-9 . _ : -`, 1 to 128 characters. Other tags get `403`.
- Tags should be unique among the items on one layer. When a row repeats inside the queue window,
  add an occurrence suffix, for example `<rowId>:2`. On duplicates, commands act on the item
  nearest to air.
- Untagged items work as in stock CasparCG. They make no events.

### 3.2 When is an item "on air"?

On the tick that shows its first frame. The event `frame` is the channel frame counter of that tick
(ticks since the server started; one tick of an interlaced channel is two fields).

- **Queued AUTO item:** the frame after the last frame of the item before it. The producer was
  created earlier, so it is ready. This is exact.
- **Take (`PLAY` of a new clip):** the decoder needs some frames to start (about 4 frames under
  emulation on the laptop). Stock CasparCG keeps the old item on screen until the new one is ready.
  The fork reports `offair` (old) and `onair` (new) on the tick that shows the new item's first frame.
- **Item replaced before it showed a frame:** it never went on air. It gets no events, and its
  on-air actions do not run.
- **Transitions (`MIX 25` and others):** `onair` and `offair` are reported when the transition
  starts. The outgoing item gets no `ended` event.

`ended` is reported on the tick after the item's last frame. For an AUTO hand-over this is the same
tick as the next item's `onair`, in the order `ended`, `offair`, `onair`.

### 3.3 The wait queue

Each layer has a background slot (stock) and, behind it, a queue (fork). The background is the head
of the queue:

```
on air          background (next)     queue
[ A ]  <------  [ B  AUTO ]  <------  [ C AUTO ] [ D AUTO ]
```

When B goes on air, C moves into the background at once. The engine plays A, B, C, D back to back
with no help from the client. Each item keeps its own `AUTO` flag, `SEEK`, `LENGTH` and transition.

- `QUEUE` appends. On an empty background it fills the background. On an empty layer with `AUTO`
  it plays at once, as `LOADBG … AUTO` does.
- `LOADBG` replaces only the background. The queue behind it stays.
- `PLAY` and `LOAD` with a clip are takes: they drop the background and the queue (§6).
- `PLAY 1-10` without a clip promotes the background. The queue then moves up.
- Producers of queued items are created when they are queued, so they are ready at the cut. Keep
  the queue short (PlayOut: 2 to 3 items). Each queued clip holds an open decoder.

### 3.4 On-air actions

`ONAIR 1-10 <tag> <command>` attaches an AMCP command to the item `<tag>` on layer 10. The command
runs when the item goes on air. There are two kinds:

| Kind | Commands | When the effect shows |
|---|---|---|
| **Stage** | `LOADBG`, `LOAD`, `PLAY`, `PAUSE`, `RESUME`, `STOP`, `CLEAR <ch>-<layer>`, `CALL`, `CALLBG`, `MIXER <property> <values>`, `MIXER CLEAR` | On the item's first frame. The command is checked and its producers are created when the `ONAIR` arrives. The recorded stage operations run inside the tick in which the item goes on air, before any layer is drawn. |
| **Deferred** | `CG ADD`, `CG PLAY`, `CG STOP`, `CG NEXT`, `CG REMOVE`, `CG CLEAR`, `CG UPDATE`, `CG INVOKE`; and `PLAY`/`LOAD`/`LOADBG` of a live input (`DECKLINK`, `BLUEFISH`, `[NDI]`, `ndi://`) | Starts right after that tick, on a separate thread. CG commands work on the channel directly, so they cannot be recorded. A live input is opened only then, never when the `ONAIR` arrives (it could be minutes early, while another producer still holds the input). Expect one or more frames, plus the template load or device open time. |

Not allowed (reply `403`): other commands, `SWAP`, `CLEAR` of a whole channel, `MIXER COMMIT`,
`MIXER MASTERVOLUME`, MIXER queries (no values), `DEFER`, and any command for another channel.

Several `ONAIR` lines for one tag run in the order received: stage commands first, then deferred
commands. A failed deferred command logs a warning and adds an `actionfailed` event.

If the item is already on air, the command runs at once (reply `202 ONAIR RAN`). If the item is a
take that has not shown its first frame yet, the command waits for that frame (`202 ONAIR OK`).

### 3.5 Fallback and failover

`FALLBACK 1-10 <clip> LOOP` creates a producer once and keeps it warm. It goes on air in these cases:

| Case | What happens | Event |
|---|---|---|
| The on-air item ended and no AUTO item waits | The fallback goes on air on the next frame (no freeze). | `onair <fallback> (ended)` |
| The decoder stalls: frames came, then none for `STALL` ticks | The AUTO background if there is one, else the fallback. | `stalled (stall)`, then `offair`/`onair (stall)` |
| A take is not ready after `STALL` ticks | The AUTO background, else the fallback. | `stalled (start)` |
| The producer throws an exception | The AUTO background, else the fallback, else stock behaviour (the layer empties). | `offair`/`onair (error)` |

- While the fallback is on air, an AUTO item (`QUEUE … AUTO`, `LOADBG … AUTO`) replaces it on the
  next frame. `PLAY` also replaces it.
- `STOP` and `CLEAR` never start the fallback: they are explicit operator actions.
- The configuration belongs to the layer index. It survives `CLEAR`. A video-mode change removes it.
- Use `LOOP` for a clip fallback. A fallback that ends freezes on its last frame.
- `STALL` is counted in ticks (25 per second on a 1080i50 channel). `0` turns stall detection off.
  The stall check works without a fallback clip too: then a stall skips to the AUTO background.

### 3.6 Events

Each layer index has an event log: the last 128 events, kept across `CLEAR`. Every event also goes
to the CasparCG log at info level, for example:

```
[event] 1-10 #12 onair 0b6c…e1 (auto) frame 48211
```

| Kind | Meaning | Reasons |
|---|---|---|
| `onair` | The item shows its first frame on this tick. | `auto`, `take`, `ended`, `stall`, `error` (how it came on air) |
| `offair` | The item stops being on air on this tick. | `auto`, `take`, `stop`, `clear`, `ended`, `stall`, `error` |
| `ended` | The item showed its last frame on the tick before. | `ended` |
| `stalled` | The item delivered no frame for `STALL` ticks, or did not start. | `stall`, `start` |
| `actionfailed` | A deferred on-air command failed. | the command and its code, for example `CG ADD 404` |

`seq` counts per layer index and starts at 1 after each server start. `instance` (in `EVENTS` and
`QUEUE INFO`) is the server start time in ms. A new `instance` means the server restarted and the
queue is gone.

---

## 4. AMCP reference

Reply codes are stock AMCP, plus `409 … CONFLICT` for a failed `EXPECT`.

### 4.1 TAG on PLAY, LOAD, LOADBG

```
PLAY   1-10 <clip> [parameters] TAG <tag>
LOAD   1-10 <clip> [parameters] TAG <tag>
LOADBG 1-10 <clip> [parameters] [AUTO] TAG <tag>
```

`TAG <tag>` can be anywhere after the clip name. It is removed before the producer sees the
parameters.

### 4.2 QUEUE

```
QUEUE 1-10 <clip> [parameters] [transition] [AUTO] [TAG <tag>] [EXPECT <tag>]
```

Appends an item. Replies: `202 QUEUE OK`, `409 QUEUE CONFLICT`, `404 QUEUE FAILED` (file not
found), `403 QUEUE FAILED` (bad parameter).

```
QUEUE 1-10 CLEAR [AFTER <tag>] [EXPECT <tag>]
```

Without `AFTER`: removes the background and the queue. With `AFTER <tag>`: keeps the waiting items
up to and including `<tag>` and removes the rest (`AFTER <on-air tag>` removes all). Reply
`201 QUEUE CLEAR OK` and the number removed on the next line, `404` when `<tag>` is not on the
layer, or `409`.

```
QUEUE 1-10 REMOVE <tag> [EXPECT <tag>]
```

Removes one waiting item. When it was the background, the next queued item moves up. Replies
`202`, `404`, `409`.

```
QUEUE 1-10 INFO
```

Reply `201 QUEUE INFO OK` and one line of JSON:

```json
{"channel":1,"layer":10,"instance":1792065829887,"frame":553,"event_seq":14,
 "foreground":{"tag":"a","producer":"ffmpeg","frame":5,"frames":50,"paused":false,"starting":false},
 "waiting":[{"tag":"b","producer":"transition","auto":true},{"tag":"c","producer":"transition","auto":true}],
 "fallback":{"set":true,"tag":"slate","active":false,"stall":50}}
```

- `foreground` is `null` when the layer does not exist. `frames` is `-1` for an endless producer.
  `frame` and `frames` count fields on an interlaced channel, as `SEEK` and `LENGTH` do.
- `starting` is `true` for a take that has not shown its first frame yet.
- `waiting` lists the background first, then the queue.

**EXPECT.** `EXPECT <tag>` runs the command only when `<tag>` is the layer's foreground (the
logical one: a starting take counts). `EXPECT -` means "no tagged item on air". The check runs on
the stage thread, between two ticks, so it cannot race a cut.

### 4.3 ONAIR

```
ONAIR 1-10 <tag> <command line>
```

Examples:

```
ONAIR 1-10 0b6c…e1 CLEAR 1-31
ONAIR 1-10 0b6c…e1 PLAY 1-31 "ratings/k12"
ONAIR 1-10 0b6c…e1 MIXER 1-31 FILL 0.86 0.04 0.1 0.1
ONAIR 1-10 0b6c…e1 CG 1-32 ADD 1 "playout/advisory" 1 "{\"text\":\"…\"}"
```

Replies:

| Reply | Meaning |
|---|---|
| `202 ONAIR OK` | Attached. It runs when the item goes on air. |
| `202 ONAIR RAN` | The item was already on air. It ran now. |
| `404 ONAIR FAILED` | The tag is not on the layer, or the command's file was not found. |
| `403 ONAIR FAILED` | The command is not allowed (§3.4), uses another channel, or failed its own check. |

### 4.4 FALLBACK

```
FALLBACK 1-10 <clip> [parameters, for example LOOP] [TAG <tag>] [STALL <ticks>]
FALLBACK 1-10 CLEAR          (removes the clip; the STALL limit stays)
FALLBACK 1-10 STALL <ticks>  (0 = off)
```

The default tag of the fallback is `fallback`. Replies: `202`, `404` (file not found), `403`.

### 4.5 EVENTS

```
EVENTS 1-10 [<seq>]
```

Reply `201 EVENTS OK` and one line of JSON with the events after `<seq>` (all kept events without
it), oldest first:

```json
{"channel":1,"layer":10,"instance":1792065829887,"frame":620,"last":9,"oldest":1,
 "events":[{"seq":8,"frame":421,"time":1792066165620,"kind":"ended","tag":"c","reason":"ended"},
           {"seq":9,"frame":430,"time":1792066165980,"kind":"offair","tag":"c","reason":"clear"}]}
```

When `<seq>` is lower than `oldest - 1`, events were lost from the log (more than 128 since).

---

## 5. OSC reference

New addresses. They appear only on layers that use the features.

| Address | Arguments | When |
|---|---|---|
| `/channel/N/stage/frame` | int64 | Every tick: the channel frame counter. |
| `/channel/N/stage/layer/L/foreground/tag` | string | The foreground is tagged. |
| `/channel/N/stage/layer/L/background/tag`, `…/background/auto` | string, bool | The background is tagged. |
| `/channel/N/stage/layer/L/queue/tags` | string… | Items wait behind the background. |
| `/channel/N/stage/layer/L/fallback/tag`, `…/fallback/active` | string, bool | A fallback is set. |
| `/channel/N/stage/layer/L/event/seq` | int64 | The layer has events: the last `seq`. |
| `/channel/N/stage/layer/L/event/<kind>` | int64 seq, int64 frame, int64 time_ms, string tag, string reason | The latest event of each kind. |

The OSC sender sends the latest state only: when it is slower than the channel, it drops states.
For that reason the latest event of each kind is sent again on every tick ("latched"). A lost or
dropped packet delays an event; it never loses it. A client should:

1. Keep the last `seq` it processed for each layer.
2. When `event/seq` is higher: process the latched events in `seq` order.
3. When `seq` values are missing (more than one event of a kind between two packets), read
   `EVENTS 1-10 <last seq>` over AMCP.

---

## 6. Behaviour changes against stock 2.5.1

- **`PLAY`/`LOAD` with a clip drop the waiting items** (background and queue). Stock `PLAY <clip>`
  also leaves no background behind, so untagged clients see no difference.
- **AUTO, end and failover checks run once per tick, before any layer is drawn** (stock: inside the
  draw of the first field). The timing is the same: the checks used the same values in both places.
- **`CLEAR` keeps the event log and the fallback configuration** of the layer index.
- **New log lines** at info level: `[event] …`, and warnings for stalls and failovers.
- **ffmpeg `frame_number()`** never wraps before the first frame (§7).
- **Console input on Windows.** When stdin is not a console (a closed pipe, a file, no handle) and a
  read fails, the console thread stops and logs `Console input closed.` Stock retried at once
  without pause and used a whole CPU core (measured 0.99 CPU-s/s at idle). A real console still
  retries, so Ctrl+C does not end its input (upstream 8a98feaf). Linux stopped at end-of-file
  already (#1406).
- **`EXIT`/`Q` without a console** no longer runs `pause` after the shutdown. `pause` would open a
  console window and wait for a key.
- No change to untagged OSC output, mixer, consumers, CG, transitions or the config file.

---

## 7. Upstream bug fixed: SEEK + AUTO skipped the clip

`ffmpeg_producer::frame_number()` returns `time() - start()`. Before the first frame of a clip
played with `SEEK n`, `time()` is 0 and `start()` is `n`. The difference is negative and wrapped to
about 4 billion as `uint32_t`. The layer's AUTO check computes
`frames_left = nb_frames - frame_number - delta`, so a background loaded with `AUTO` in that window
went on air at once, and the clip that was just taken was skipped.

This explains the "premature AUTO" that PlayOutVue PR #5 worked around: it arms `LOADBG … AUTO` only
after the taken clip has rendered its first frame. The fork clamps the value to 0. The tests in §9
send `PLAY … SEEK 50 TAG p` and `LOADBG … AUTO TAG q` in one TCP write: q now goes on air exactly
`LENGTH` frames after p, on a progressive and on a 1080i50 channel.

PR #18 fixed a **different** race, and this fix does not cover it: `refreshQueue` sent
`LOADBG C AUTO` while B waited, unaired, in the background, so B was overwritten and skipped. One
background slot plus a client that rewrites it cannot be made safe. `QUEUE` with `EXPECT` fixes that
one (§8.2): the engine owns the order of the next items, and a stale edit gets `409`.

The fix is its own commit (§11).

---

## 8. How PlayOut uses the fork

### 8.1 What moves from PlayOut into the engine

| PlayOut today | With the fork |
|---|---|
| `caspar.rs` infers the end of a clip from OSC `file/time` and `file/path`, gated by `auto_advance_not_before_ms`, and emits `caspar://advance`. | The engine advances by itself. PlayOut reads `event/onair` with the row id. |
| `preloadNextItemAt` waits for the on-air clip's first frame before `LOADBG … AUTO` (`preloadOnAirGate`). | Not needed: the queue plus the fix in §7. |
| `refreshQueue` re-issues `LOADBG` on rundown edits and can overwrite an unaired background. | `QUEUE CLEAR AFTER … EXPECT …` and `QUEUE … EXPECT …`. A stale edit gets `409`. |
| `applyComplianceForPlayback` sends `CLEAR`/`PLAY`/`CG ADD` after the advance event round-trips through the WebView, so graphics land after the cut. | `ONAIR` lines per item, sent with the `QUEUE`. Rating and TP show on the item's first frame. |
| `is_premature_switch_observation` and basename comparisons (sub-clips share their parent's file). | Not needed: tags identify the row, not the file. |
| A dry queue freezes on the last frame or goes black. | `FALLBACK … LOOP STALL …` |
| Recovery re-adopts the engine state from `INFO` and OSC. | `QUEUE INFO` and `EVENTS <seq>`. `instance` shows an engine restart. |

### 8.2 Recipes

**Connect or start.**

```
QUEUE 1-10 INFO                      -> 400 ERROR: stock server, keep the old code path
FALLBACK 1-10 "playout/emergency" LOOP TAG fallback STALL 50
QUEUE 1-10 INFO                      -> resync: what is on air, what waits
EVENTS 1-10 <last seq>               -> as-run of the time PlayOut was away (same instance only)
```

`playout/emergency` is an example: an emergency clip (or slate image) that PlayOut deploys to the
media folder. `STALL 50` is 2 seconds on a 25-tick channel.

**Take row X (operator, frame-exact).** Load first, attach the actions, then take:

```
QUEUE 1-10 CLEAR
LOADBG 1-10 "<path X>" SEEK <in> LENGTH <len> TAG <X>
ONAIR 1-10 <X> CLEAR 1-31
ONAIR 1-10 <X> CLEAR 1-32
ONAIR 1-10 <X> CLEAR 1-34
ONAIR 1-10 <X> PLAY 1-31 "<rating image>"
ONAIR 1-10 <X> MIXER 1-31 FILL <x> <y> <sx> <sy>
ONAIR 1-10 <X> PLAY 1-34 "<TP image>"
ONAIR 1-10 <X> CG 1-32 ADD 1 "playout/advisory" 1 "<json>"
PLAY 1-10
QUEUE 1-10 "<path X+1>" SEEK .. LENGTH .. AUTO TAG <X+1> EXPECT <X>
ONAIR 1-10 <X+1> …                  (its graphics)
QUEUE 1-10 "<path X+2>" SEEK .. LENGTH .. AUTO TAG <X+2> EXPECT <X>
ONAIR 1-10 <X+2> …
```

`PLAY 1-10 "<path X>" TAG <X>` followed by the `ONAIR` lines also works. The actions then wait for
X's first frame if they arrive during the decoder start, which is usual but not certain.

**Natural advance.** PlayOut sends nothing to make it happen. On `onair <tag>`:

1. Set `currentKey` to the row with that id.
2. Refill the queue to its depth: `QUEUE … AUTO TAG <next> EXPECT <tag>` plus its `ONAIR` lines.

**Rundown edit while on air.**

1. `QUEUE 1-10 INFO`. Compare `waiting` with the new order of the next rows.
2. Keep the longest common start. `QUEUE 1-10 CLEAR AFTER <last kept> EXPECT <on-air tag>` (or
   `QUEUE 1-10 CLEAR EXPECT <on-air tag>` when nothing is kept).
3. Queue the rest with `EXPECT <on-air tag>`.
4. On `409`: the cut happened during the edit. Read `QUEUE INFO` again and repeat.

`QUEUE` only appends. To change an item in the middle (for example a new trim), clear after the
item before it and queue the tail again.

**Live rows (layer 20 above layer 10). Not tested on hardware yet.** Keep the timing in the layer-10
queue. PlayOut opens the input itself on layer 20's background when the live row becomes the next
row, and an on-air action shows it on the row's first frame:

```
QUEUE 1-10 "playout/transparent" LENGTH <fields> AUTO TAG <live row>     (a transparent PNG)
LOADBG 1-20 DECKLINK 1 FORMAT 1080i5000          (PlayOut, when the live row becomes next)
ONAIR 1-10 <live row> PLAY 1-20                  (promotes layer 20's background: frame-exact)
ONAIR 1-10 <next row> CLEAR 1-20                 (on the row after it)
```

- `playout/transparent` is a fully transparent PNG that PlayOut would deploy. Image and DeckLink
  producers accept `LENGTH`. Check the exact length on the broadcast PC: the image producer counts
  received fields, so a progressive channel can end one frame early.
- Two live rows in a row on the same input: do not load layer 20 again, and do not clear it between
  them. A second producer cannot open an input that the first one still holds.
- `ONAIR … PLAY 1-20 DECKLINK 1` also works, but it is deferred (§3.4): the input opens after the cut,
  so the picture comes some frames late.
- To test on the broadcast PC: a live row between two clips, two live rows in a row, a live row with
  no input signal (the take may stay `starting`; with `STALL` it fails over).

**Stop.** `CLEAR 1-10` (event `offair … (clear)`), then clear the graphics layers. The fallback stays
configured but does not start.

**PlayOut crash and relaunch.** The engine keeps playing the queue. On relaunch, `QUEUE INFO` tells
which row is on air (`foreground.tag`) and what waits. `EVENTS <last seq>` gives what aired
meanwhile. The relaunch planner can adopt the state directly, with no OSC inference.

**Engine crash.** `instance` changes, the queue and the log are gone. PlayOut's existing relaunch and
resume flow (PR #21) plays the row again. With tags it re-adopts by id.

### 8.3 PlayOut code that would change (not done yet)

These files are PlayOut's protected core. Each change needs the owner's approval.

- `src-tauri/src/amcp.rs`: add `QUEUE`, `ONAIR`, `FALLBACK`, `EVENTS` to the command allowlist; add
  `TAG`/`EXPECT` to the `LOADBG`/`PLAY` builders; add `QUEUE`/`ONAIR` builders.
- `src-tauri/src/caspar.rs`: parse `…/event/*`, `…/foreground/tag` and `/stage/frame`; emit
  `caspar://onair {uuid, frame, time}` from events; remove the OSC advance inference and the
  premature-switch detector on fork servers.
- `src/services/caspar.ts`: the take, natural-advance and preload paths become the recipes above;
  `applyComplianceForPlayback` becomes an `ONAIR` builder; remove the first-frame gates on fork
  servers.
- Settings: fallback clip, `STALL` ticks, queue depth.
- Keep the stock code path for a stock server (`QUEUE 1-10 INFO` answers `400`).

### 8.4 Starting and stopping the engine (patch ready, not applied)

This change is written and tested, but PlayOutVue stays untouched until the integration. It is
kept as `tools/playout-integration/caspar_process-stdin-exit.patch` (base: PlayOutVue `6fa3e38`).
With it, `src-tauri/src/caspar_process.rs` (not protected core):

- starts `casparcg.exe` with a stdin pipe and keeps it open while the engine runs. With stdin at
  end-of-file, stock 2.5.1 spins one CPU core (§6); with the pipe open both servers idle at about
  0.02 CPU-s/s;
- stops it gracefully: it writes `EXIT` to stdin as **UTF-16LE** (the engine sets stdin to
  `_O_U16TEXT` on Windows, so plain ASCII arrives as other characters), closes the pipe, waits up to
  5 s, and only then terminates the process. Measured: the fork exits in 0.4 s, stock in 0.6 s, both
  with code 0 and `Successfully shutdown` in the log. `restart` still terminates at once.

If PlayOut closes while the engine keeps running (keep-alive), the pipe closes: the fork then stops
its console thread; stock spins again.

### 8.5 Relation to the emergency player

The fallback covers failures inside a running engine: a dry queue, a stalled decoder, a failed
producer. It does not cover a dead engine, a dead GPU or a dead PC. The planned separate DeckLink
emergency player (and a main/backup chain with an SDI changeover) is still needed for those.

---

## 9. Tests

The live test kit is in `tools/playout-tests`. It needs Python 3 and ffmpeg (`FFMPEG` environment
variable, `PATH`, or `C:\ffmpeg\bin\ffmpeg.exe`).

```powershell
powershell -File tools\playout-tests\run_all.ps1          # builds media, starts the server, runs all
```

How it checks: every test clip shows its source frame index as seven black/white blocks and its
identity as a chroma block. The server records its own output with the FILE consumer. The scripts
decode the recording and check each output frame against the event log: which clip, which source
frame, which graphics.

| Script | Checks | What it covers |
|---|---|---|
| `harness.py` | 112 | Progressive 50p channel: TAG, QUEUE with SEEK/LENGTH, every queued item starts on its event frame and plays each source frame once, on-air actions on the first frame of b and c, `ONAIR RAN`, the take recipe, EXPECT `409`, REMOVE, unknown tag `404`, refused commands `403`, fallback after the last item, fallback yields to AUTO, the §7 fix (q − p = 40 frames), CLEAR events, EVENTS since, OSC latch. Interlaced PAL channel: queue with LENGTH/SEEK in fields, field-exact. **1080i5000 channel** (the broadcast format): queue, on-air actions on the item's first field, fallback on the field after the last one, the §7 fix. |
| `extra.py` | 31 | `BEGIN`/`COMMIT` with QUEUE and ONAIR, PLAY drops the queue, `CLEAR AFTER`, decoder stall over HTTP: 1.2 s of stream, 25 ticks without frames, then `stalled`, `offair (stall)`, `onair slate (stall)`. `ONAIR … PLAY 1-20 DECKLINK 1` is accepted without opening a device and runs after the cut (it fails there: the laptop has no card). |
| `regress.py` | 22 | Untagged use shows no new OSC addresses; every new OSC address; `INFO` carries tags; a real `CG ADD` of PlayOut's `playout/advisory.html` as a deferred on-air action. |
| `stdin_cpu.ps1` | — | Idle CPU with stdin at end-of-file against a console stdin. |
| `stdin_exit.py` | — | Starts the server as PlayOut does (no window, piped stdin): idle CPU with the pipe open, graceful `EXIT`, idle CPU after the pipe closes. Run it on the fork and on stock. |

Result on the laptop (x64 under emulation), 2026-10-15: **165 of 165 checks pass**.

`stdin_exit.py` on the laptop:

| Server | Pipe open | `EXIT` (UTF-16LE), pipe closed | Pipe closed without `EXIT` |
|---|---|---|---|
| fork | 0.02 CPU-s/s | exits in 0.4 s, code 0 | 0.01 CPU-s/s, keeps running |
| stock 2.5.1 | 0.03 CPU-s/s | exits in 0.6 s, code 0 | 1.01 CPU-s/s, keeps running |

Notes:
- Custom interlaced video modes cannot be used for tests: the server's cadence check wants samples
  per frame, while built-in interlaced modes use samples per field, so audio runs at twice the rate.
  The interlaced test uses the built-in `PAL` mode.
- On an interlaced channel the FILE consumer records one full picture per field (50 per second).

---

## 10. Limits and open points

- **Not tested on DeckLink hardware.** The laptop has no card. Test on the broadcast PC: DeckLink
  output, the live recipe, and `is_ready()` of the DeckLink producer when there is no input signal
  (a take of a live input may stay `starting` until a signal comes).
- **The error failover is not tested live.** A producer cannot be made to throw on demand. The path
  was reviewed only. Its events can be stamped one frame late, because the failover happens during
  the draw and events are stamped before it.
- **The "start" stall was not tested live** (a take that never becomes ready).
- **CG on-air commands are not frame-exact** (§3.4).
- **`SWAP`** moves layers without events. A swapped layer keeps its fallback setting, the index does not.
- **Tags must be unique per layer** for exact results (§3.1).
- **Long soak test not done.** The 165 checks run for about two minutes. Each queued item holds an
  open decoder, and the engine does not limit the queue depth yet.
- **Upstream fixes after v2.5.1 are merged** (`origin/v2.5.x`, merge `520596b2`: CEF crashes, the
  channel monitor snapshot race, DeckLink v210 colours and others). They touch none of the fork's
  files. After the merge the build is clean and all 165 checks pass. Merge `origin/v2.5.x` again
  when upstream adds fixes, and re-run the tests.

---

## 11. Source map and commits

| File | Change |
|---|---|
| `src/core/producer/layer_item.h` | New: `item_info`, `item_actions`, `fallback_config`, `layer_event`, `layer_snapshot`. |
| `src/core/producer/layer.h`, `layer.cpp` | Tags, the queue, `prepare()` (AUTO, end, stall, fallback, visible switch), events, attach of actions. |
| `src/core/producer/stage.h`, `stage.cpp` | The tick runs `prepare_layers()` before drawing and applies on-air actions there; per-index event log and fallback configuration; `stage_sync_ops`, `invoke_sync()`, `stage_capture`. |
| `src/protocol/amcp/AMCPCommandsImpl.cpp` | `TAG` on `PLAY`/`LOAD`/`LOADBG`; `QUEUE*`, `ONAIR`, `FALLBACK*`, `EVENTS`; JSON replies. |
| `src/protocol/amcp/amcp_command_context.h` | The executor for deferred on-air commands. |
| `src/protocol/amcp/AMCPCommand.h` | `layer_index()` accessor. |
| `src/modules/ffmpeg/producer/ffmpeg_producer.cpp` | The §7 fix. |
| `src/shell/main.cpp`, `platform_specific.h`, `windows_specific.cpp`, `linux_specific.cpp` | Console input: stop without a console, no `pause` after `EXIT` without a console (§6). |
| `src/core/CMakeLists.txt` | Lists `layer_item.h`. |
| `tools/playout-tests/` | The live test kit. |

This is a private fork: nothing goes to the official CasparCG repository. The change set is one
commit per feature on branch `playout/2.5.1`, in this order; each commit builds on its own, and the
full test kit runs on the last one:

1. `fix(ffmpeg)`: the §7 clamp.
2. `fix(shell)`: console input without a console (§6).
3. `feat(core)`: item tags and the per-layer event log.
4. `feat(core)`: the wait queue with EXPECT.
5. `feat(core)`: on-air actions.
6. `feat(core)`: fallback and stall failover.
7. `docs`: this document and `tools/playout-tests`.

CasparCG is GPL-3. If the fork is given to anyone outside the organisation, its source must be
offered too. PlayOut talks to it over the network and is not affected.
