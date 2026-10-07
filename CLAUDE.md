# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Four standalone macOS scripts for keeping Time Machine backups small. See README.md for user-facing usage.

- **`tm-exclude.sh`**: Finds and registers build artifacts, caches, and toolchain directories as Time Machine path-based exclusions (`tmutil addexclusion -p`). Reads search roots from `~/.config/tm-exclude/search-roots`. Plans all changes as the user, then applies them in a single `sudo` invocation.
- **`tm-prune-exclusions.py`**: Finds redundant exclusions (paths already covered by a parent exclusion) and removes them. Reads manual roots from `~/.config/tm-exclude/tm-exclusions.manual`, the same file `tm-exclude.sh` adds exclusions from. The two parsers (`strip_comment` in Python, `load_manual_paths` in bash) must stay in sync: `#` is a comment only at line start or after whitespace.
- **`tm-delta.py`**: A per-user LaunchAgent (`com.corykim.tm-delta`, every 60s) runs `--watch`, which polls `tmutil status -X` during a backup and appends the final `Progress.bytes`/`files` to `~/Library/Logs/tm-delta/history.csv`. No root. The history stores only start/end/bytes/files/last_phase; completion and the backup name are derived at display time by matching a `SnapshotDates` entry in the system TM plist within the row's start-1m..end+2m window.
- **`tm-diff.py`**: Diffs two backups and opens a curses browser. `local` source: walks the two matching local APFS snapshots in Python (size + mtime, reports `st_blocks*512`). `network` source: mounts both backups from the share and parses `tmutil compare -X`. Results are cached in `/Library/Caches/tm-diff`.

Cross-script coupling: `tm-diff.py` (running as root) reads `tm-delta.py`'s history CSV from `$SUDO_USER`'s home, and matches rows to its snapshot names with the same window rule as `tm-delta.py`'s `match_backup`. Change both if that format or rule changes. `tm-exclude.sh` writes to the system plist; `tm-prune-exclusions.py` reads the user plist by default.

Verified facts about this Mac (macOS 26.6.2), don't re-derive:
- `backupd` writes nothing under the `com.apple.TimeMachine` log subsystem and no copy summaries anywhere, so don't build on `log show`.
- `tmutil status -X` phases on a good backup are `MountingDiskImage`, `PreparingSourceVolumes`, `FindingChanges` (has `ChangedItemCount`), `Copying` (has `Progress`), `Finishing`, `ThinningPostBackup`, then `Running` turns false. `Progress.totalBytes`/`totalFiles` are whole-volume figures, and `Percent` doesn't reach 1.
- The snapshot name equals the time the `Finishing` phase starts. `DateOfStateChange` is UTC and marks roughly when the backup started.
- `/Library/Preferences/com.apple.TimeMachine.plist` and `tmutil latestbackup` need Full Disk Access. Interactive terminals have it, but the LaunchAgent's Python does not, so the agent can only use `tmutil status`. Test agent-context behavior with a temporary `launchctl submit` job, not from the terminal.
- The agent starts once a minute and `FindingChanges` lasts about 4 seconds, so `ChangedItemCount` is almost never seen. That's why it isn't recorded.
- `sudo` on this Mac goes through an endpoint-privilege "Confirm Execution Yes/No" prompt, so `sudo -n` and the `!` prefix can't run root commands. Ask the user to run them in their own terminal, writing output to a file.

- `tmutil compare` refuses two local snapshots ("source volume descendant") and needs at least one side inside a backup, which is why local mode has its own walker. It also crashes with `NSInvalidArgumentException: URL is nil` on some folders (seen on `~/Library/Logs` as a non-root user). `-X` output: `Changes[]` entries with `AddedItem`, `RemovedItem`, or `OlderItem`+`NewerItem`+`Differences`; each item has `Path` and, for files, an exact `Size`. `AddedItem` is inferred, not yet seen.
- `backupd` keeps many local snapshots mounted under `/Volumes/com.apple.TimeMachine.localsnapshots/Backups.backupdb/<host>/<stamp>/Data`, readable without root from a Full Disk Access shell. Use them to test the local walker (load the module with `importlib`, call `walk_local`).
- Backup `N` pairs with the newest local snapshot in `(N-1, N]`, because the local snapshot is taken at backup start and the backup is named when it finishes.
- `~/Library/Application Support/com.apple.container` holds sparse 550 GB-apparent container images (about 1.4 GB on disk). A running container's `rootfs.ext4` changes every backup, so `tm-exclude.sh` excludes the whole `containers/` folder in `FIXED_PATHS`; `content/` and `snapshots/` (images and layers) are deliberately left in.

Test `tm-delta.py` without a live backup by loading it with `importlib` and passing a fake `get` to `watch()`.

## Running the scripts

```bash
# Dry-run (show what would change, no writes)
./tm-exclude.sh --dry-run
./tm-prune-exclusions.py

# Apply
./tm-exclude.sh
./tm-prune-exclusions.py --apply

# Custom search roots file
./tm-exclude.sh --roots-file ~/my-roots
```

`tm-prune-exclusions.py` useful flags: `--verbose`, `--debug`, `--include-system`, `--skip-xattr`, `--backup`, `--verify`.

## Requirements

- macOS only (`tmutil`, `PlistBuddy`, `mdfind`)
- Terminal (or whichever shell runs the scripts) needs **Full Disk Access**
- Run as your normal user — `tm-exclude.sh` calls `sudo` internally; `tm-prune-exclusions.py --prune-system` requires a separate `sudo` invocation
- Python 3 for `tm-prune-exclusions.py` (no third-party packages; stdlib only)

## Key design constraints

- `tm-exclude.sh` uses **one `sudo` call** for all changes; individual path args are passed as positional arguments, never interpolated into script text, so paths with spaces and special characters are safe.
- Exclusion deduplication in `tm-exclude.sh` is in-memory: `CURRENT_EXCLUSIONS` is updated after every planned add so that child paths are correctly detected as covered before the batch is applied.
- `tm-prune-exclusions.py` reads plists with `plistlib` (not `PlistBuddy`) for safe structured access; it never checks whether a path exists before calling `tmutil removeexclusion` — stale paths are intentionally handled.
- `SkipPaths` in `/Library/Preferences/com.apple.TimeMachine.plist` stores path-based exclusions. `tmutil` has no `list` verb for these; `tm-exclude.sh` reads them via `PlistBuddy -c "Print :SkipPaths"`.
