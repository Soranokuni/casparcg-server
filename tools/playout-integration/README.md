# PlayOut changes held for the integration

PlayOutVue changes that go with this fork. They are kept here until the PlayOut integration is
done (`PLAYOUT-FORK.md` §8.3, §8.4), so PlayOutVue itself stays untouched until then.

| Patch | Base | What it does |
|---|---|---|
| `caspar_process-stdin-exit.patch` | PlayOutVue `6fa3e38` (main) | `src-tauri/src/caspar_process.rs`: starts the engine with a stdin pipe that stays open, and stops it gracefully with `EXIT` (UTF-16LE), then terminates it only after 5 s. Adds a unit test. Tested 2026-10-15: clippy `-D warnings` clean, 183 tests pass. |

Apply from the PlayOutVue root:

```powershell
git apply --check D:\casparcg-fork\tools\playout-integration\caspar_process-stdin-exit.patch
git apply D:\casparcg-fork\tools\playout-integration\caspar_process-stdin-exit.patch
```
