# tm-utils

macOS scripts for keeping Time Machine backups small. Two of them manage exclusions, so backups skip things that can be rebuilt or re-downloaded (build output, package caches, toolchains, VM disks). The other two show how much each backup copied and what changed, so you can tell whether the exclusions are working.

| Script | Job |
|---|---|
| `tm-exclude.sh` | Finds rebuildable directories and adds them as Time Machine exclusions. Also removes exclusions under `$HOME` whose paths no longer exist. |
| `tm-prune-exclusions.py` | Finds exclusions that are redundant because a parent directory is already excluded, and removes them. |
| `tm-delta.py` | Records how much data each backup copied, using a LaunchAgent that watches `tmutil status`, and shows the history. |
| `tm-diff.py` | Diffs a backup against the one before it, using local snapshots when they still exist (fast) or the backups on the share, and opens a folder browser of what changed, sorted by size. |

The exclusion scripts don't change anything until you ask: `tm-exclude.sh` has `--dry-run`, and `tm-prune-exclusions.py` is a dry run unless you pass `--apply`. The reporting scripts only read, apart from their own history and cache files.

## Requirements

- macOS (uses `tmutil`, `/usr/libexec/PlistBuddy`, `mdfind`, `diskutil`, `mount_apfs`)
- Full Disk Access for the terminal app running the scripts, so `find` can see protected folders
- Python 3 (standard library only) for the `.py` scripts
- Root for `tm-diff.py` (mounting snapshots). It re-runs itself under `sudo` if needed, so you get one prompt. `tm-delta.py` runs as your normal user.

## tm-exclude.sh

```bash
./tm-exclude.sh --dry-run                 # show what would change
./tm-exclude.sh                           # apply
./tm-exclude.sh --roots-file ~/my-roots   # use a different search roots file
./tm-exclude.sh --manual-file ~/my-list   # use a different manual exclusions file
```

Run it as your normal user, not with `sudo`. It plans everything as you, then applies all adds and removes in one `sudo` call, so you get one password prompt. If you decline the prompt, nothing changes.

### What it excludes

**Fixed paths**, skipped if they don't exist:

- Xcode `DerivedData`
- npm, Yarn, and uv caches
- Maven `~/.m2/repository`, Gradle `~/.gradle/caches`
- Cargo registry and rustup toolchains (`~/.cargo/bin` is kept)
- pyenv, asdf, nvm, and rbenv installs
- Terraform global plugin cache
- kubectl and AWS CLI caches
- Docker Desktop VM disk
- Apple `container` per-container disk images (`~/Library/Application Support/com.apple.container/containers`). Container state isn't restored; images and layers are still backed up.
- Google, Krisp, and Microsoft Edge updater caches
- The Chrome Canary app bundle
- Chrome Service Worker caches for every profile
- Tanium and Microsoft Defender agent data (redeployed by IT; a restored machine may show these agents unhealthy until IT reinstalls them)

**Project directories** found under your search roots, up to 6 levels deep:

`node_modules`, `.next`, `.nuxt`, `.gradle`, `__pycache__`, `.terraform`, `dist`, `build`, `out`, `target`

`dist`, `build`, `out`, and `target` are generic names. Run `--dry-run` first and check that nothing you care about matches.

Once a directory matches, `find` doesn't descend into it, so nested matches (like a `node_modules` inside a `node_modules`) aren't added separately.

### Search roots file

Default location is `~/.config/tm-exclude/search-roots`. Override it with `--roots-file` or the `SEARCH_ROOTS_FILE` environment variable. One directory per line, `#` comments allowed, a leading `~` is expanded:

```
~/code
~/work   # client repos
```

The script exits with an error if the file is missing.

### Manual exclusions file

For paths specific to your machine that don't fit a project pattern, such as one container's disk image, list them in `~/.config/tm-exclude/tm-exclusions.manual`. Override the location with `--manual-file` or the `MANUAL_FILE` environment variable. The file is optional. `tm-prune-exclusions.py` reads the same file, so a path listed here is excluded by one script and treated as a parent exclusion by the other.

Each line is one file or folder, and a leading `~` means your home folder. `#` starts a comment at the start of a line or after a space or tab; a `#` inside a path, like `/a/b#c`, is kept. Paths must be absolute after `~` expansion; others are skipped as `[not absolute]`.

```
# Large VM image for a project, rebuilt from its repo
~/vms/dev-box.qcow2
```

These are checked after the fixed paths and before the project patterns, using the same duplicate, covered and not-found checks.

### How it decides what to add

Path exclusions live in the `SkipPaths` array of `/Library/Preferences/com.apple.TimeMachine.plist`. `tmutil` can't list these, so the script reads them with PlistBuddy. A candidate is skipped if:

- it doesn't exist (`[not found]`)
- it's already excluded (`[duplicate]`)
- a parent directory is already excluded, including one queued earlier in the same run (`[covered]`)

It also queues removal of existing exclusions that are under `$HOME` and no longer exist on disk. Entries outside `$HOME` are left alone.

At the end it prints counts for added, removed, duplicates, covered, not found, and failed. It exits non-zero if anything failed. Re-running is safe, since anything already added shows up as a duplicate.

## tm-prune-exclusions.py

```bash
./tm-prune-exclusions.py                  # dry run, prints what it would remove
./tm-prune-exclusions.py --verbose        # dry run with full detail on stderr
./tm-prune-exclusions.py --apply          # remove redundant exclusions
```

It gathers exclusions from these sources, keeping only paths under `$HOME`:

- **User plist**: `SkipPaths` in `~/Library/Preferences/com.apple.TimeMachine.plist`
- **System plist**: `/Library/Preferences/com.apple.TimeMachine.plist`, only with `--include-system`
- **Sticky (xattr) exclusions**: folders tagged with `com_apple_backup_excludeItem`, found with `mdfind`. Limited to `$HOME` unless you pass `--all`. Skip these entirely with `--skip-xattr`.
- **Manual roots**: paths listed in `~/.config/tm-exclude/tm-exclusions.manual`, the same file `tm-exclude.sh` reads (see [Manual exclusions file](#manual-exclusions-file)), or the file given with `--manual-roots-file`. These count as parents for coverage checks but are never removed.

It combines these into a minimal set of root exclusions, then marks any user, xattr, or (with `--prune-system`) system entry that sits under one of those roots as redundant. Redundant entries are removed one at a time with `tmutil removeexclusion`.

Note that `tm-exclude.sh` writes to the system plist. To prune those entries, pass `--include-system --prune-system` and run with `sudo` (the script checks for write access to the system plist and exits if it doesn't have it).

### Options

| Flag | Effect |
|---|---|
| `--apply` | Actually remove entries. Without it, the script only reports. |
| `--include-system` | Read the system plist as well |
| `--prune-system` | Also remove redundant system plist entries (needs `sudo`) |
| `--all` | Search for xattr exclusions across all of Spotlight, not just `$HOME` (`/System` and `/Library` excluded) |
| `--skip-xattr` | Ignore xattr exclusions |
| `--backup` | With `--apply`, copy the plists to timestamped `.bak` files first |
| `--verify` | After removing, re-scan and report anything still redundant |
| `--verbose` | Log raw and filtered inputs and the computed roots |
| `--debug` | Also log raw plist contents and `tmutil` output |
| `--log-file FILE` | Send logs to a file instead of stderr |
| `--manual-roots-file FILE` | Use a different manual roots file (default `~/.config/tm-exclude/tm-exclusions.manual`) |

Logs go to stderr and the final summary goes to stdout. After an `--apply` that removes something, it suggests restarting `backupd`.

## tm-delta.py

```bash
./tm-delta.py --install          # install the LaunchAgent (do this once)
./tm-delta.py                    # show the last 20 recorded backups
./tm-delta.py --last 50          # show more
./tm-delta.py --threshold-gb 0.5 # flag backups over 0.5 GB (default 1.0)
./tm-delta.py --uninstall        # remove the LaunchAgent; history is kept
```

No `sudo` needed. Each row shows the backup name, whether it completed, files copied, bytes copied, how long it took, hours since the previous backup, and GB per hour. Rows over the threshold are flagged.

The GB/hour rate is the column to watch. A big backup after a long gap (for example, after the share was unreachable for a few hours) is expected. A high rate on a backup that ran on schedule means something is churning.

### How it works

`backupd` on macOS 26 doesn't log per-backup copy totals, so the script reads them from `tmutil status -X` while the backup runs. The LaunchAgent (`com.corykim.tm-delta`) runs `tm-delta.py --watch` every minute. If no backup is running it exits at once. If one is running, it samples the status every 5 seconds until the backup ends, then appends a row to `~/Library/Logs/tm-delta/history.csv`.

- **Copied bytes and files** are `Progress.bytes` and `Progress.files` at the end of the Copying phase. `Progress.totalBytes` and `totalFiles` cover the whole source volume, not the backup, so they're ignored. `Percent` is unreliable too: it ended at 0.67 on a backup that finished.
- **Complete or incomplete** is decided when you run `tm-delta.py`, not when the agent records the row. A row is complete if a `SnapshotDates` entry in `/Library/Preferences/com.apple.TimeMachine.plist` falls between its start and end. That entry gives the backup its name (for example `2026-10-06-184609`), the same name `tm-diff.py` uses. Otherwise the row is incomplete (failed or cancelled).
- **Why not in the agent:** that plist, and `tmutil latestbackup`, need Full Disk Access. Your terminal has it, but the LaunchAgent's Python doesn't, and granting it to a general-purpose Python would extend it to every script that Python runs. If you run `tm-delta.py` from somewhere without Full Disk Access, the status shows `unknown`.
- **Since prev** is measured to the previous `SnapshotDates` entry, so it stays correct even if the watcher missed earlier backups.

Sizes use decimal units (1 GB = 10^9 bytes). The LaunchAgent runs the script from wherever it was, with whichever Python installed it, so re-run `--install` if either moves. Its output and errors go to `~/Library/Logs/tm-delta/agent.log`.

Only backups that run while the agent is installed get recorded. There's no way to recover sizes for earlier backups.

## tm-diff.py

```bash
./tm-diff.py                         # list backups, pick one, browse it
./tm-diff.py latest                  # browse the most recent backup
./tm-diff.py latest --path ~         # only your home folder
./tm-diff.py latest --path ~ -I node_modules -I .git   # skip anything with these names
./tm-diff.py 2026-10-03-0317         # any unique prefix of a backup name
./tm-diff.py --list                  # list only (no compare)
./tm-diff.py latest --base 3         # compare against an older backup instead of the previous one
./tm-diff.py latest --source network # force the slow network diff
./tm-diff.py latest --print 4 --hide-excluded   # text tree, 4 levels deep
./tm-diff.py --clear-cache
```

You can name a backup by list number (1 is the latest), by a unique prefix of its name, or with `latest`. The list shows:

- the gap since the previous backup;
- the copied size from `tm-delta.py` history, in 1024-based units;
- **Local**: whether both backups still have local snapshots, so the diff will be fast;
- **Cached**: whether a diff with the same `--path` and `-I` options is already cached.

The backup list comes from `SnapshotDates` in the Time Machine plist, so listing doesn't touch the network share.

### Two ways to diff

**Local (the default when possible, fast).** Time Machine takes an APFS snapshot of your internal disk when each backup starts, then copies from it. These are named like `com.apple.TimeMachine.2026-10-07-141930.local` and last about 24 hours. A backup is paired with the newest local snapshot taken after the previous backup finished. For example, backup `2026-10-07-143344` (started 14:19) pairs with local snapshot `141930`.

`tmutil compare` refuses to compare two local snapshots ("Can't compare a source volume descendant to a source volume descendant"), so the script walks both trees itself, comparing each file's size and modification time. It reuses snapshots Time Machine already has mounted and mounts the rest under `/private/tmp/tm-diff-mnt`. Items covered by current exclusions are skipped entirely, the way Time Machine skips them. On this Mac, a walk of the home folder (about 134,000 folders) takes about 1.5 minutes.

**Network (the fallback, slow).** For older backups, the script mounts both backups from the `Backups of ...` volume on the share and runs `tmutil compare -X -s -t`. That reads every file in both backups over SMB, so it can take hours for the whole disk. `--path` helps a lot here.

`--source auto` (the default) uses local when both backups have a local snapshot, and network otherwise. `--source local` fails instead of falling back.

Each result is saved in `/Library/Caches/tm-diff`, keyed by the pair, source, `--path`, `-I` names and (network only) `--flags`, so later views open instantly. A progress line shows elapsed time and changes found so far. Ctrl+C unmounts anything the script mounted and caches nothing.

### Narrowing the diff

- `--path PATH` compares one folder on the Data volume, such as `~`, `~/code`, `/Applications` or `/Library`. `~` expands to your home folder even under `sudo`. The browser opens at that folder.
- `-I NAME` (repeatable) skips every file or folder with exactly that name, anywhere in the tree. It takes a name, not a path: `-I node_modules` works, but `-I ~/code/node_modules` is rejected. Use `--path` to choose a folder.

### Browser

| Key | Action |
|---|---|
| up/down, `k`/`j` | Move |
| right, enter, `l` | Open folder |
| left, backspace, `h` | Go up |
| PgUp/PgDn, `g`/`G` | Page, jump to top/bottom |
| `s` | Sort by size or name |
| `x` | Hide or show items covered by current exclusions |
| `q`, Esc | Quit |

Folder sizes roll up from their contents and items are sorted largest first. Added items are green, changed yellow, removed red. Items covered by a current path exclusion are marked `X`; hiding them recalculates the totals, so what's left is what's still being backed up.

### Caveats

- **Sizes differ by source.** Local diffs show bytes on disk. Network diffs show the exact sizes `tmutil compare -X` reports, which is apparent size. For sparse files, such as VM and container disk images, apparent size can be hundreds of GB more than what's on disk. A 550 GB container `rootfs.ext4` uses about 1.4 GB.
- For a changed file, the size shown is the whole file, not what Time Machine transferred. Delta copies can transfer less.
- Local diffs list only changed files, not folders whose own modification time changed. Network diffs list both.
- `X` marks only appear in network diffs, because local diffs skip excluded items. `X` only checks path exclusions (`SkipPaths` in the system plist), not sticky (xattr) exclusions.
- `tmutil compare` crashes (`NSInvalidArgumentException: URL is nil`) on some folders. When that happens the script says so; narrow the diff with `--path` or skip the folder with `-I`.
- Mounting a snapshot Time Machine is using may fail as busy. Wait for the current backup to finish.
