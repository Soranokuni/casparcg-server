# PlayOut fork — live tests

Live checks for the fork features (TAG, QUEUE, ONAIR, FALLBACK/STALL, EVENTS) and the SEEK/AUTO fix.
See `PLAYOUT-FORK.md` §9 in the repository root for what each script covers.

```powershell
powershell -File tools\playout-tests\run_all.ps1 [-ExeDir build\shell\Release] [-PlayOutDir D:\PlayOut]
```

`run_all.ps1` writes `casparcg.config` from the template, makes the test media once, copies PlayOut's
advisory template, starts the server hidden on AMCP port 5260 / OSC port 6260, runs `harness.py`,
`extra.py` and `regress.py`, and stops the server. Each script writes a `.log` next to it.

Other tools:

- `dump.py rec1.mkv [width height] [first last]` prints the decoded timeline of a recording.
- `selfcheck.py` checks the decoder against the source clips.
- `stdin_cpu.ps1` measures idle CPU with stdin at end-of-file against a console stdin.
- `stdin_exit.py <folder> [<folder> ...]` starts each server as PlayOut does (no window, piped stdin)
  and checks idle CPU, a graceful `EXIT` (sent as UTF-16LE) and idle CPU after the pipe closes.

Needs Python 3 and ffmpeg (`FFMPEG`, `PATH`, or `C:\ffmpeg\bin\ffmpeg.exe`).
