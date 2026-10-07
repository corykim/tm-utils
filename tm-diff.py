#!/usr/bin/env python3
"""
tm-diff.py: browse what changed in a Time Machine backup.

Diffs a backup against the one before it, caches the result, and opens an
interactive folder browser sorted by size. Items now covered by a path
exclusion are marked X, and can be hidden to show only what is still being
backed up.

Two ways to diff (--source, default auto):
  local    Compare the local APFS snapshots Time Machine took on this Mac at
           the start of each backup. Fast (internal SSD), but local snapshots
           only last about 24 hours. tmutil compare refuses to compare two
           local snapshots, so this walks both trees itself, comparing each
           file's size and modification time, and skips current exclusions.
  network  Mount both backups from the destination and run
           `tmutil compare -X`. Works for any backup, but walks every file
           over the network share, so it is slow.
  auto     local when both backups still have a local snapshot, else network.

Usage:
  tm-diff.py                      pick a backup from a list, then browse
  tm-diff.py 2026-10-03-031716    browse that backup (any unique prefix works)
  tm-diff.py latest               browse the most recent backup
  tm-diff.py latest --path ~      only compare your home folder
  tm-diff.py latest -I node_modules -I .git   skip paths containing these names
  tm-diff.py --list               list backups (cached diffs, sizes from tm-delta)
  tm-diff.py TARGET --base OTHER  diff against a backup other than the previous one
  tm-diff.py TARGET --print 3     print the tree 3 levels deep instead of browsing
  tm-diff.py --clear-cache

Browser keys:
  up/down or k/j   move          enter/right/l   open folder
  left/h/backspace go up         PgUp/PgDn       page
  s                sort by size or name
  x                hide or show items covered by current exclusions
  q                quit

Notes:
  - Needs root to mount snapshots; re-runs itself under sudo (one prompt).
  - Each diff is cached in /Library/Caches/tm-diff, keyed by the pair,
    source, --path, -I names and (network only) --flags.
  - --path limits the diff to one folder on the Data volume, e.g. ~ or
    /Applications. -I takes a name, not a path: it skips every file or folder
    with that exact name anywhere in the tree.
  - Network diffs use `tmutil compare -s -t` (size and modification time) by
    default; change with --flags (see `man tmutil`). Local diffs always
    compare size and modification time.
  - For a changed (!) file, the size is the file's size, not the amount Time
    Machine transferred, which can be smaller for delta-copied files.
  - The X marker checks path-based exclusions (SkipPaths) only, not sticky
    (xattr) exclusions. Local diffs skip excluded items entirely, so X only
    appears in network diffs.
"""

import argparse
import csv
import curses
import hashlib
import json
import os
import plistlib
import pwd
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

VERSION = "2.0"
CACHE_DIR = "/Library/Caches/tm-diff"
MOUNT_ROOT = "/private/tmp/tm-diff-mnt"
HISTORY_REL = "Library/Logs/tm-delta/history.csv"
TM_PLIST = "/Library/Preferences/com.apple.TimeMachine.plist"
DATA_VOL = "/System/Volumes/Data"
STAMP = "%Y-%m-%d-%H%M%S"

SNAP_RE = re.compile(r'com\.apple\.TimeMachine\.(\d{4}-\d{2}-\d{2}-\d{6})\.backup')
LOCAL_RE = re.compile(r'com\.apple\.TimeMachine\.(\d{4}-\d{2}-\d{2}-\d{6})\.local')
MOUNT_LINE_RE = re.compile(r'^com\.apple\.TimeMachine\.(\d{4}-\d{2}-\d{2}-\d{6})\.local@\S+ on (.+) \(')


# ─── Helpers ────────────────────────────────────────────────────────────────

def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def human(n: float) -> str:
    for unit, div in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{int(n)}B"


def stamp_dt(stamp: str) -> datetime:
    return datetime.strptime(stamp, STAMP)


def user_home() -> str:
    u = os.environ.get("SUDO_USER")
    if u:
        try:
            return pwd.getpwnam(u).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


# ─── Destination and snapshots ──────────────────────────────────────────────

def find_backup_volume(explicit: Optional[str]) -> str:
    if explicit:
        if not os.path.ismount(explicit):
            sys.exit(f"Not a mounted volume: {explicit}")
        return explicit

    def scan() -> List[str]:
        try:
            names = os.listdir("/Volumes")
        except OSError:
            return []
        return [os.path.join("/Volumes", n) for n in sorted(names)
                if n.startswith("Backups of ") and os.path.ismount(os.path.join("/Volumes", n))]

    vols = scan()
    if not vols:
        # tmutil listbackups usually mounts the destination as a side effect.
        print("Backup volume not mounted; asking Time Machine to mount it...", file=sys.stderr)
        run(["tmutil", "listbackups"])
        vols = scan()
    if not vols:
        sys.exit("No 'Backups of ...' volume is mounted. Check that the backup share\n"
                 "is reachable, open Time Machine's browser once, or pass --volume.")
    if len(vols) > 1:
        print(f"Several backup volumes mounted; using {vols[0]} (override with --volume).",
              file=sys.stderr)
    return vols[0]


def volume_device(vol: str) -> str:
    r = run(["df", vol])
    lines = r.stdout.strip().splitlines()
    if r.returncode != 0 or len(lines) < 2:
        sys.exit(f"Couldn't find the device for {vol}: {r.stderr.strip()}")
    return lines[-1].split()[0]


def list_snapshots(vol: str) -> List[str]:
    r = run(["diskutil", "apfs", "listSnapshots", vol])
    if r.returncode != 0:
        sys.exit(f"diskutil apfs listSnapshots failed: {r.stderr.strip() or r.stdout.strip()}")
    return sorted(set(SNAP_RE.findall(r.stdout)))


def backup_stamps() -> List[str]:
    """Backup names from SnapshotDates in the TM plist, oldest first.

    Same names as the destination's snapshots, but reading them doesn't
    need the network share.
    """
    try:
        with open(TM_PLIST, "rb") as f:
            pl = plistlib.load(f)
    except Exception as e:
        sys.exit(f"Can't read {TM_PLIST}: {e}")
    stamps = set()
    for dest in pl.get("Destinations", []):
        for d in dest.get("SnapshotDates", []):
            if isinstance(d, datetime):
                # plistlib returns naive UTC; backup names are local time.
                stamps.add(d.replace(tzinfo=timezone.utc).astimezone().strftime(STAMP))
    return sorted(stamps)


def local_snapshots() -> List[str]:
    r = run(["tmutil", "listlocalsnapshots", "/"])
    return sorted(set(LOCAL_RE.findall(r.stdout)))


def local_for(stamp: str, stamps: List[str], locals_: List[str]) -> Optional[str]:
    """The local snapshot a backup was copied from, if it still exists.

    Time Machine takes the local snapshot when a backup starts and names the
    backup when it finishes, so it's the latest local snapshot after the
    previous backup and no later than this one.
    """
    i = stamps.index(stamp)
    lo = stamps[i - 1] if i else ""
    hits = [s for s in locals_ if lo < s <= stamp]
    return hits[-1] if hits else None


def history_sizes(snaps: List[str]) -> Dict[str, int]:
    """Copied bytes per snapshot name from tm-delta history, if it exists.

    A history row matches the snapshot taken between its start and end, the
    same rule tm-delta.py uses (match_backup).
    """
    sizes: Dict[str, int] = {}
    path = os.path.join(user_home(), HISTORY_REL)
    if not os.path.exists(path):
        return sizes
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                lo = datetime.fromisoformat(r["start"]) - timedelta(minutes=1)
                hi = datetime.fromisoformat(r["end"]) + timedelta(minutes=2)
                total = int(r["bytes"])
            except (KeyError, ValueError, TypeError):
                continue
            hits = [s for s in snaps if lo <= stamp_dt(s) <= hi]
            if hits:
                sizes[hits[-1]] = total
    return sizes


# ─── Options ────────────────────────────────────────────────────────────────

class Opts:
    def __init__(self, source: str, flags: List[str], sub: str, ignore: List[str],
                 volume: Optional[str]):
        self.source = source
        self.flags = flags
        self.sub = sub            # Data-volume-relative, no leading slash; "" = whole volume
        self.ignore = sorted(set(ignore))
        self.volume = volume


def resolve_subtree(arg: Optional[str]) -> str:
    """--path as a Data-volume-relative path ('' for the whole volume)."""
    if not arg:
        return ""
    p = arg
    if p == "~" or p.startswith("~/"):
        p = user_home() + p[1:]
    p = os.path.abspath(p)
    if p == DATA_VOL or p.startswith(DATA_VOL + "/"):
        p = p[len(DATA_VOL):] or "/"
    return p.strip("/")


def check_subtree(roots: List[str], sub: str) -> None:
    if not sub:
        return
    for r in roots:
        if not os.path.isdir(os.path.join(r, sub)):
            sys.exit(f"--path /{sub} isn't a folder in {r}.\n"
                     "It must be on the Data volume (e.g. ~, /Users, /Applications, /Library)\n"
                     "and must exist in both backups.")


# ─── Mounting ───────────────────────────────────────────────────────────────

def unmount(mp: str) -> None:
    if os.path.ismount(mp):
        if run(["umount", mp]).returncode != 0:
            run(["umount", "-f", mp])
    try:
        os.rmdir(mp)
    except OSError:
        pass


def mount_snapshot(snap_name: str, dev: str, mp_name: str) -> str:
    mp = os.path.join(MOUNT_ROOT, mp_name)
    os.makedirs(mp, exist_ok=True)
    unmount(mp)  # leftover from an interrupted run
    os.makedirs(mp, exist_ok=True)
    r = run(["mount_apfs", "-o", "ro", "-s", snap_name, dev, mp])
    if r.returncode != 0:
        sys.exit(f"Couldn't mount {snap_name}: {r.stderr.strip() or r.stdout.strip()}\n"
                 "If a backup is running, wait for it to finish and try again.")
    return mp


def mounted_locals() -> Dict[str, str]:
    """Local snapshots Time Machine already has mounted: stamp -> mount point."""
    out: Dict[str, str] = {}
    for line in run(["mount"]).stdout.splitlines():
        m = MOUNT_LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def backup_volumes(mp: str, stamp: str) -> Dict[str, str]:
    root = os.path.join(mp, f"{stamp}.backup")
    try:
        names = os.listdir(root)
    except OSError as e:
        sys.exit(f"Unexpected snapshot layout at {root}: {e}")
    return {n: os.path.join(root, n) for n in names
            if not n.startswith(".") and os.path.isdir(os.path.join(root, n))}


def with_progress(label: str, status, work):
    done = threading.Event()
    start = time.time()

    def ticker() -> None:
        while not done.wait(1.0):
            e = int(time.time() - start)
            sys.stderr.write(f"\r  {label}: {e // 60}m{e % 60:02d}s, {status()}   ")
            sys.stderr.flush()

    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    try:
        return work()
    finally:
        done.set()
        t.join()
        sys.stderr.write("\n")


# ─── Network diff (tmutil compare -X) ──────────────────────────────────────

def strip_prefix(path: str, prefixes: List[str]) -> str:
    for pre in prefixes:
        if path.startswith(pre + "/") or path == pre:
            return path[len(pre):] or "/"
    return path


def parse_compare_xml(data: bytes, prefixes: List[str]) -> List[list]:
    """Entries [op, bytes, size text, differences, volume-relative path]."""
    entries: List[list] = []
    for c in plistlib.loads(data).get("Changes", []):
        if "AddedItem" in c:
            op, item, tags = "+", c["AddedItem"], ""
        elif "RemovedItem" in c:
            op, item, tags = "-", c["RemovedItem"], ""
        elif "NewerItem" in c and "OlderItem" in c:
            op, item, tags = "!", c["NewerItem"], ", ".join(c.get("Differences", []))
        elif "NewerItem" in c:
            op, item, tags = "+", c["NewerItem"], ""
        elif "OlderItem" in c:
            op, item, tags = "-", c["OlderItem"], ""
        else:
            continue
        size = int(item.get("Size", 0) or 0)
        entries.append([op, size, human(size), tags,
                        strip_prefix(item.get("Path", ""), prefixes)])
    return entries


def run_compare(p1: str, p2: str, opts: Opts, label: str, prefixes: List[str]) -> List[list]:
    cmd = ["tmutil", "compare", "-X"] + opts.flags
    for name in opts.ignore:
        cmd += ["-I", name]
    cmd += [p1, p2]
    # stderr goes to a file so a flood of warnings can't fill the pipe and stall tmutil.
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
    chunks: List[bytes] = []
    count = [0]
    markers = (b"<key>Differences</key>", b"<key>AddedItem</key>", b"<key>RemovedItem</key>")

    def work() -> int:
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)
            if any(m in line for m in markers):
                count[0] += 1
        return proc.wait()

    try:
        rc = with_progress(f"comparing {label}", lambda: f"{count[0]} changes so far", work)
    except BaseException:
        proc.kill()
        raise
    if rc != 0:
        err.seek(0)
        msg = err.read().decode(errors="replace").strip()
        if "NSInvalidArgumentException" in msg:
            sys.exit("tmutil compare crashed (a known tmutil bug on some folders).\n"
                     "Narrow the diff with --path, or skip the folder with -I NAME.")
        sys.exit(f"tmutil compare failed (rc={rc}): {msg[-2000:]}")
    return parse_compare_xml(b"".join(chunks), prefixes)


def compute_network(base: str, target: str, opts: Opts) -> dict:
    vol = find_backup_volume(opts.volume)
    on_dest = set(list_snapshots(vol))
    for s in (base, target):
        if s not in on_dest:
            sys.exit(f"Backup {s} isn't on {vol}.")
    dev = volume_device(vol)
    mounts = []
    try:
        print(f"Mounting backups {base} and {target} read-only...", file=sys.stderr)
        mb = mount_snapshot(f"com.apple.TimeMachine.{base}.backup", dev, base); mounts.append(mb)
        mt = mount_snapshot(f"com.apple.TimeMachine.{target}.backup", dev, target); mounts.append(mt)
        vb, vt = backup_volumes(mb, base), backup_volumes(mt, target)
        common = sorted(set(vb) & set(vt))
        if opts.sub:
            common = [v for v in common if v == "Data"]
        if not common:
            sys.exit(f"No volume folders in common: {sorted(vb)} vs {sorted(vt)}")
        entries: List[list] = []
        for v in common:
            rb, rt = vb[v], vt[v]
            check_subtree([rb, rt], opts.sub)
            p1 = os.path.join(rb, opts.sub) if opts.sub else rb
            p2 = os.path.join(rt, opts.sub) if opts.sub else rt
            prefixes = sorted({rb, rt, os.path.realpath(rb), os.path.realpath(rt)},
                              key=len, reverse=True)
            rows = run_compare(p1, p2, opts, v, prefixes)
            if len(common) > 1 or v != "Data":
                for r in rows:
                    r[4] = f"/[{v}]{r[4]}"
            entries.extend(rows)
        return {"base": base, "target": target, "source": "network", "flags": opts.flags,
                "path": opts.sub, "ignore": opts.ignore,
                "computed": datetime.now().isoformat(timespec="seconds"),
                "volumes": common, "entries": entries}
    finally:
        for mp in mounts:
            unmount(mp)


# ─── Local diff (walk two local snapshots) ─────────────────────────────────

def listing(root: str, rel: str) -> Dict[str, Tuple[bool, int, int, int]]:
    """name -> (is_dir, size, mtime_ns, bytes on disk), without following symlinks.

    Bytes on disk matter for sparse files such as VM and container disk
    images, whose apparent size can be hundreds of GB more than they use.
    """
    out = {}
    with os.scandir(os.path.join(root, rel) if rel else root) as it:
        for e in it:
            st = e.stat(follow_symlinks=False)
            out[e.name] = (stat.S_ISDIR(st.st_mode), st.st_size, st.st_mtime_ns,
                           st.st_blocks * 512)
    return out


def walk_local(old_root: str, new_root: str, sub: str, excl: "Exclusions",
               ignore: Set[str], state: dict) -> List[list]:
    """Diff two Data-volume trees by size and mtime.

    Entries match parse_compare_xml, but sizes are bytes on disk rather than
    apparent size. Directories whose only change is their own mtime aren't
    listed. Excluded and ignored items are skipped entirely, like Time
    Machine does.
    """
    entries: List[list] = state["entries"]

    def skip(name: str, vpath: str) -> bool:
        return name in ignore or excl.covers_exact(vpath)

    def add_all(op: str, root: str, rel: str, info: Tuple[bool, int, int, int]) -> None:
        if not info[0]:
            entries.append([op, info[3], human(info[3]), "", "/" + rel])
            return
        stack = [rel]
        while stack:
            d = stack.pop()
            state["dirs"] += 1
            try:
                items = listing(root, d)
            except OSError:
                state["errors"] += 1
                continue
            if not items and d == rel:
                entries.append([op, 0, human(0), "", "/" + rel])
            for name, (is_dir, _, _, ondisk) in items.items():
                p = f"{d}/{name}"
                if skip(name, "/" + p):
                    continue
                if is_dir:
                    stack.append(p)
                else:
                    entries.append([op, ondisk, human(ondisk), "", "/" + p])

    stack = [sub]
    while stack:
        d = stack.pop()
        state["dirs"] += 1
        try:
            old, new = listing(old_root, d), listing(new_root, d)
        except OSError:
            state["errors"] += 1
            continue
        for name in old.keys() | new.keys():
            p = f"{d}/{name}" if d else name
            vp = "/" + p
            if skip(name, vp):
                continue
            o, n = old.get(name), new.get(name)
            if o and n:
                if o[0] and n[0]:
                    stack.append(p)
                elif o[0] or n[0]:
                    add_all("-", old_root, p, o)
                    add_all("+", new_root, p, n)
                else:
                    diffs = []
                    if o[1] != n[1]:
                        diffs.append("size")
                    if o[2] != n[2]:
                        diffs.append("mtime")
                    if diffs:
                        entries.append(["!", n[3], human(n[3]), ", ".join(diffs), vp])
            elif n:
                add_all("+", new_root, p, n)
            else:
                add_all("-", old_root, p, o)
    return entries


def compute_local(base: str, target: str, lbase: str, ltarget: str, opts: Opts,
                  excl: "Exclusions") -> dict:
    already = mounted_locals()
    mounts: List[str] = []
    roots: Dict[str, str] = {}
    dev = None
    try:
        for snap in (lbase, ltarget):
            if snap in already and os.path.isdir(already[snap]):
                roots[snap] = already[snap]
                continue
            if dev is None:
                dev = volume_device(DATA_VOL)
            mp = mount_snapshot(f"com.apple.TimeMachine.{snap}.local", dev, f"local-{snap}")
            mounts.append(mp)
            roots[snap] = mp
        check_subtree([roots[lbase], roots[ltarget]], opts.sub)
        print(f"Comparing local snapshots {lbase} and {ltarget}...", file=sys.stderr)
        state = {"dirs": 0, "errors": 0, "entries": []}
        entries = with_progress(
            "walking", lambda: f"{state['dirs']} folders, {len(state['entries'])} changes",
            lambda: walk_local(roots[lbase], roots[ltarget], opts.sub, excl,
                               set(opts.ignore), state))
        if state["errors"]:
            print(f"  {state['errors']} folders couldn't be read and were skipped.",
                  file=sys.stderr)
        return {"base": base, "target": target, "source": "local",
                "local_snapshots": [lbase, ltarget], "path": opts.sub,
                "ignore": opts.ignore,
                "computed": datetime.now().isoformat(timespec="seconds"),
                "volumes": ["Data"], "entries": entries}
    finally:
        for mp in mounts:
            unmount(mp)


# ─── Cache ──────────────────────────────────────────────────────────────────

def cache_path(base: str, target: str, source: str, opts: Opts) -> str:
    key = {"path": opts.sub, "ignore": opts.ignore}
    if source == "network":
        key["flags"] = opts.flags
    h = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:10]
    return os.path.join(CACHE_DIR, f"{base}__{target}__{source}__{h}.json")


def cached_source(base: str, target: str, opts: Opts) -> Optional[str]:
    order = {"auto": ["local", "network"], "local": ["local"], "network": ["network"]}
    for src in order[opts.source]:
        if os.path.exists(cache_path(base, target, src, opts)):
            return src
    return None


def load_or_compute(base: str, target: str, src: str, opts: Opts, compute) -> dict:
    path = cache_path(base, target, src, opts)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    data = compute()
    os.makedirs(CACHE_DIR, mode=0o755, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


# ─── Exclusions ─────────────────────────────────────────────────────────────

class Exclusions:
    def __init__(self) -> None:
        self.paths: Set[str] = set()
        home = user_home()
        try:
            with open(TM_PLIST, "rb") as f:
                pl = plistlib.load(f)
            for p in pl.get("SkipPaths", []):
                if p.startswith("~"):
                    p = home + p[1:]
                self.paths.add(p.rstrip("/") or "/")
        except Exception:
            pass
        self._memo: Dict[str, bool] = {}

    def covers(self, path: str) -> bool:
        if path in self._memo:
            return self._memo[path]
        result = False
        for candidate in (path, "/System/Volumes/Data" + path):
            p = candidate
            while p and p != "/":
                if p in self.paths:
                    result = True
                    break
                p = p.rsplit("/", 1)[0]
            if result:
                break
        self._memo[path] = result
        return result

    def covers_exact(self, path: str) -> bool:
        """Is this exact path excluded? For walkers that already pruned ancestors."""
        return path in self.paths or (DATA_VOL + path) in self.paths


# ─── Tree ───────────────────────────────────────────────────────────────────

class Node:
    __slots__ = ("name", "parent", "children", "entry", "total", "count", "path")

    def __init__(self, name: str, parent: Optional["Node"]):
        self.name = name
        self.parent = parent
        self.children: Dict[str, "Node"] = {}
        self.entry: Optional[list] = None
        self.total = 0
        self.count = 0
        self.path = "/" if parent is None else (
            ("" if parent.path == "/" else parent.path) + "/" + name)


def build_tree(entries: List[list]) -> Node:
    root = Node("", None)
    for e in entries:
        node = root
        for part in e[4].strip("/").split("/"):
            if not part:
                continue
            nxt = node.children.get(part)
            if nxt is None:
                nxt = node.children[part] = Node(part, node)
            node = nxt
        node.entry = e

    def tally(n: Node) -> None:
        # A node's own size counts only when nothing below it was listed,
        # so a folder and its contents are never counted twice.
        if n.children:
            for c in n.children.values():
                tally(c)
            n.total = sum(c.total for c in n.children.values())
            n.count = sum(c.count for c in n.children.values())
        else:
            n.total = n.entry[1] if n.entry else 0
            n.count = 1 if n.entry else 0
    tally(root)
    return root


class View:
    """Totals and child lists, optionally leaving out excluded items."""

    def __init__(self, excl: Exclusions):
        self.excl = excl
        self.hide = False
        self._memo: Dict[int, tuple] = {}

    def toggle(self) -> None:
        self.hide = not self.hide
        self._memo.clear()

    def stats(self, n: Node) -> tuple:
        """(total bytes, item count) for n under the current hide setting."""
        if not self.hide:
            return n.total, n.count
        key = id(n)
        if key not in self._memo:
            if self.excl.covers(n.path):
                r = (0, 0)
            elif n.children:
                t = c = 0
                for k in n.children.values():
                    kt, kc = self.stats(k)
                    t += kt; c += kc
                r = (t, c)
            else:
                r = (n.total, n.count)
            self._memo[key] = r
        return self._memo[key]

    def children(self, n: Node, by_name: bool) -> List[Node]:
        kids = list(n.children.values())
        if self.hide:
            kids = [k for k in kids if self.stats(k)[1] > 0]
        if by_name:
            kids.sort(key=lambda k: k.name.lower())
        else:
            kids.sort(key=lambda k: (-self.stats(k)[0], k.name.lower()))
        return kids


def print_tree(root: Node, view: View, depth: int, top: int = 15) -> None:
    excl = view.excl

    def walk(n: Node, level: int) -> None:
        kids = view.children(n, False)
        for k in kids[:top]:
            total, count = view.stats(k)
            op = k.entry[0] if (k.entry and not k.children) else " "
            x = "X" if excl.covers(k.path) else " "
            print(f"{human(total):>7}  {count:>6}  {op}{x} {'  ' * level}"
                  f"{k.name}{'/' if k.children else ''}")
            if k.children and level + 1 < depth:
                walk(k, level + 1)
        if len(kids) > top:
            print(f"{'':>7}  {'':>6}     {'  ' * level}... {len(kids) - top} more")
    walk(root, 0)


# ─── Curses browser ─────────────────────────────────────────────────────────

def browse(stdscr, root: Node, view: View, title: str) -> None:
    excl = view.excl
    curses.curs_set(0)
    try:
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, 8 if curses.COLORS > 8 else curses.COLOR_BLUE, -1)
        color = True
    except curses.error:
        color = False
    op_color = {"+": 1, "!": 2, "-": 3}

    cur = root
    sel_stack: List[int] = []
    sel, top = 0, 0
    by_name = False

    while True:
        kids = view.children(cur, by_name)
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 5)
        sel = max(0, min(sel, len(kids) - 1))
        if sel < top:
            top = sel
        elif sel >= top + body_h:
            top = sel - body_h + 1

        stdscr.erase()
        ft, fc = view.stats(cur)
        hdr = f" {title}   folder total ≈ {human(ft)}   items {fc}"
        stdscr.addnstr(0, 0, hdr.ljust(w), w - 1, curses.A_REVERSE)
        stdscr.addnstr(1, 0, f" {cur.path}", w - 1, curses.A_BOLD)
        flags = f"sort: {'name' if by_name else 'size'}   excluded: {'hidden' if view.hide else 'shown (X)'}"
        stdscr.addnstr(2, 0, f" {flags}", w - 1)

        for i, k in enumerate(kids[top:top + body_h]):
            y = 3 + i
            is_dir = bool(k.children)
            op = k.entry[0] if (k.entry and not is_dir) else " "
            x = "X" if excl.covers(k.path) else " "
            tags = f"  ({k.entry[3]})" if (k.entry and not is_dir and k.entry[3]) else ""
            kt, kc = view.stats(k)
            line = f" {human(kt):>7}  {kc:>6}  {op}{x} {k.name}{'/' if is_dir else ''}{tags}"
            attr = curses.A_REVERSE if top + i == sel else 0
            if color and x == "X":
                attr |= curses.color_pair(4)
            elif color and op in op_color:
                attr |= curses.color_pair(op_color[op])
            stdscr.addnstr(y, 0, line.ljust(w), w - 1, attr)

        if not kids:
            stdscr.addnstr(3, 2, "(nothing here)", w - 3)

        status = kids[sel].path if kids else cur.path
        stdscr.addnstr(h - 2, 0, f" {status}".ljust(w), w - 1, curses.A_DIM)
        keys = " ↑↓ move  →/⏎ open  ← up  s sort  x hide excluded  q quit"
        stdscr.addnstr(h - 1, 0, keys.ljust(w), w - 1, curses.A_REVERSE)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (ord("q"), 27):
            return
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel += 1
        elif ch in (curses.KEY_UP, ord("k")):
            sel -= 1
        elif ch == curses.KEY_NPAGE:
            sel += body_h
        elif ch == curses.KEY_PPAGE:
            sel -= body_h
        elif ch in (curses.KEY_HOME, ord("g")):
            sel = 0
        elif ch in (curses.KEY_END, ord("G")):
            sel = len(kids) - 1
        elif ch in (curses.KEY_RIGHT, ord("l"), 10, 13, curses.KEY_ENTER):
            if kids and kids[sel].children:
                sel_stack.append(sel)
                cur, sel, top = kids[sel], 0, 0
        elif ch in (curses.KEY_LEFT, ord("h"), curses.KEY_BACKSPACE, 127, 8):
            if cur.parent is not None:
                cur = cur.parent
                sel, top = (sel_stack.pop() if sel_stack else 0), 0
        elif ch == ord("s"):
            by_name = not by_name
        elif ch == ord("x"):
            view.toggle()
            sel, top = 0, 0
        elif ch == curses.KEY_RESIZE:
            pass


# ─── Main ───────────────────────────────────────────────────────────────────

def resolve(spec: str, snaps: List[str]) -> str:
    if spec == "latest":
        return snaps[-1]
    if spec.isdigit() and int(spec) < 1000:
        i = int(spec)
        if 1 <= i <= len(snaps):
            return snaps[-i]
    matches = [s for s in snaps if s.startswith(spec)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"No backup matches {spec!r}. Use --list to see backups.")
    sys.exit(f"{spec!r} matches {len(matches)} backups; be more specific.")


def print_list(snaps: List[str], locals_: List[str], opts: Opts, limit: int) -> None:
    hist = history_sizes(snaps)
    print(f"{'#':>3}  {'Backup':<19}  {'Since prev':>10}  {'Copied*':>9}  {'Local':<5}  Cached")
    shown = list(enumerate(snaps))[-limit:]
    for i, s in reversed(shown):
        prev = snaps[i - 1] if i else None
        gap = f"{(stamp_dt(s) - stamp_dt(prev)).total_seconds() / 3600:.1f} h" if prev else "-"
        size = hist.get(s)
        size_s = human(size) if size is not None else "-"
        local = "yes" if prev and local_for(s, snaps, locals_) and local_for(prev, snaps, locals_) else ""
        cached = (cached_source(prev, s, opts) or "") if prev else ""
        print(f"{len(snaps) - i:>3}  {s:<19}  {gap:>10}  {size_s:>9}  {local:<5}  {cached}")
    print("\n* from tm-delta history, when available. Pick by # or by name."
          "\nLocal = both backups still have a local snapshot, so the diff is fast."
          "\nCached = a diff for this pair with the same --path and -I options exists.")


def main() -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(description="Browse what changed in a Time Machine backup.")
    ap.add_argument("target", nargs="?", help="backup name, unique prefix, list #, or 'latest'")
    ap.add_argument("--base", help="compare against this backup instead of the previous one")
    ap.add_argument("--list", action="store_true", help="list backups and exit")
    ap.add_argument("--limit", type=int, default=20, help="backups to show in lists (default 20)")
    ap.add_argument("--source", choices=["auto", "local", "network"], default="auto",
                    help="diff local snapshots or the backups on the destination (default auto)")
    ap.add_argument("--path", metavar="PATH",
                    help="only compare this folder on the Data volume, e.g. ~ or /Applications")
    ap.add_argument("-I", "--ignore", action="append", default=[], metavar="NAME",
                    help="skip files and folders with this exact name (repeatable)")
    ap.add_argument("--print", type=int, metavar="DEPTH", dest="print_depth",
                    help="print the tree to DEPTH levels instead of browsing")
    ap.add_argument("--hide-excluded", action="store_true",
                    help="start with excluded items hidden (browser and --print)")
    ap.add_argument("--flags", default="-s -t",
                    help="tmutil compare flags for network diffs (default '-s -t'; see man tmutil)")
    ap.add_argument("--volume", help="backup volume mount point, if auto-detect picks wrong")
    ap.add_argument("--clear-cache", action="store_true", help="delete cached diffs and exit")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = ap.parse_args()

    for name in args.ignore:
        if "/" in name or not name:
            sys.exit(f"-I takes a file or folder name, not a path: {name!r}. "
                     "Use --path to limit the diff to a folder.")

    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable, os.path.realpath(__file__)] + sys.argv[1:])

    if args.clear_cache:
        n = 0
        if os.path.isdir(CACHE_DIR):
            for f in os.listdir(CACHE_DIR):
                if f.endswith(".json"):
                    os.remove(os.path.join(CACHE_DIR, f)); n += 1
        print(f"Removed {n} cached diffs.")
        return

    opts = Opts(args.source, shlex.split(args.flags), resolve_subtree(args.path),
                args.ignore, args.volume)
    snaps = backup_stamps()
    if len(snaps) < 2:
        sys.exit(f"Need at least two backups; found {len(snaps)}.")
    locals_ = local_snapshots() if opts.source != "network" else []

    if args.list:
        print_list(snaps, locals_, opts, args.limit)
        return

    target_spec = args.target
    if not target_spec:
        print_list(snaps, locals_, opts, args.limit)
        try:
            target_spec = input("\nBackup to browse [1 = latest]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print()
            return
    target = resolve(target_spec, snaps)
    if args.base:
        base = resolve(args.base, snaps)
    else:
        i = snaps.index(target)
        if i == 0:
            sys.exit(f"{target} is the oldest backup; there's nothing before it to compare.")
        base = snaps[i - 1]
    if stamp_dt(base) >= stamp_dt(target):
        sys.exit(f"--base {base} must be older than {target}.")

    excl = Exclusions()
    lbase = local_for(base, snaps, locals_)
    ltarget = local_for(target, snaps, locals_)
    src = cached_source(base, target, opts)
    if src is None:
        if lbase and ltarget:
            src = "local"
        elif opts.source == "local":
            missing = base if not lbase else target
            sys.exit(f"Backup {missing} no longer has a local snapshot (they last about "
                     "24 hours).\nUse --source network or --source auto.")
        else:
            src = "network"
            if opts.source == "auto":
                print("No local snapshots for this pair; using the network backups (slow).",
                      file=sys.stderr)
    if src == "local":
        compute = lambda: compute_local(base, target, lbase, ltarget, opts, excl)
    else:
        compute = lambda: compute_network(base, target, opts)
    try:
        data = load_or_compute(base, target, src, opts, compute)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted; snapshots unmounted, nothing cached.")

    root = build_tree(data["entries"])
    start = root
    for part in opts.sub.split("/") if opts.sub else []:
        if part not in start.children:
            break
        start = start.children[part]
    view = View(excl)
    if args.hide_excluded:
        view.toggle()
    scope = f"/{opts.sub}" if opts.sub else "whole disk"
    title = f"{base} → {target}  [{src}, {scope}]"
    if args.print_depth:
        t, c = view.stats(start)
        print(f"{title}   total ≈ {human(t)}   items {c}\n")
        print_tree(start, view, args.print_depth)
        return
    if not data["entries"]:
        print(f"No changes found between {base} and {target} ({src}, {scope}).")
        return
    curses.wrapper(browse, start, view, title)


if __name__ == "__main__":
    main()
