#!/usr/bin/env python3
"""
tm-delta.py: report how much data each Time Machine backup copied.

backupd doesn't log per-backup copy totals on current macOS, so this samples
`tmutil status -X` while a backup runs and records the final byte and file
counts from its Copying phase. A per-user LaunchAgent checks once a minute;
when a backup is running it polls every few seconds until the backup ends,
then appends one row to the history file. No sudo needed.

Usage:
  tm-delta.py                    show the last 20 recorded backups
  tm-delta.py --last 50          show more
  tm-delta.py --threshold-gb 0.5 flag backups larger than 0.5 GB (default 1.0)
  tm-delta.py --install          install the LaunchAgent (runs --watch every minute)
  tm-delta.py --uninstall        remove it (history is kept)
  tm-delta.py --watch            record the running backup, if any, then exit

What gets recorded:
  bytes, files   Progress.bytes / Progress.files at the end of the Copying
                 phase. Progress.totalBytes and totalFiles describe the whole
                 source volume, not the backup, so they're ignored.
  start, end     When the backup started (DateOfStateChange) and when the
                 watcher saw it stop.

Which backup a row belongs to is worked out when you view the history, not
when it's recorded: a row is complete if a SnapshotDates entry in
/Library/Preferences/com.apple.TimeMachine.plist falls between its start and
end, and takes that entry's name (e.g. 2026-10-06-184609, the same names
tm-diff.py uses). The plist is protected by Full Disk Access, which your
terminal has and the LaunchAgent doesn't, so the agent can't do this itself.

"Since prev" and GB/hour use the previous entry in SnapshotDates, so they're
right even for backups the watcher didn't see.
"""

import argparse
import csv
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

VERSION = "2.0"
LABEL = "com.corykim.tm-delta"
HOME = os.path.expanduser("~")
AGENT_PLIST = os.path.join(HOME, "Library/LaunchAgents", f"{LABEL}.plist")
LOG_DIR = os.path.join(HOME, "Library/Logs/tm-delta")
DEFAULT_HISTORY = os.path.join(LOG_DIR, "history.csv")
AGENT_LOG = os.path.join(LOG_DIR, "agent.log")
TM_PLIST = "/Library/Preferences/com.apple.TimeMachine.plist"
STAMP = "%Y-%m-%d-%H%M%S"

FIELDS = ["start", "end", "bytes", "files", "last_phase"]
EARLY_PHASES = {"Starting", "MountingDiskImage", "PreparingSourceVolumes", "FindingChanges"}
LATE_PHASES = {"Finishing", "ThinningPostBackup"}


# ─── Time Machine state ─────────────────────────────────────────────────────

def get_status() -> Optional[dict]:
    r = subprocess.run(["tmutil", "status", "-X"], capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return plistlib.loads(r.stdout)
    except Exception:
        return None


def snapshot_stamps() -> Optional[List[str]]:
    """Completed backup names from SnapshotDates, local time, oldest first.

    None if the plist can't be read (no Full Disk Access).
    """
    try:
        with open(TM_PLIST, "rb") as f:
            pl = plistlib.load(f)
    except Exception:
        return None
    stamps = set()
    for dest in pl.get("Destinations", []):
        for d in dest.get("SnapshotDates", []):
            if isinstance(d, datetime):
                # plistlib returns naive UTC; snapshot names are local time.
                stamps.add(d.replace(tzinfo=timezone.utc).astimezone().strftime(STAMP))
    return sorted(stamps)


def parse_state_change(st: dict) -> Optional[datetime]:
    """DateOfStateChange is a UTC string like '2026-10-06 23:29:01'."""
    v = st.get("DateOfStateChange")
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    if isinstance(v, str):
        try:
            dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    return None


# ─── Watching ───────────────────────────────────────────────────────────────

class Record:
    def __init__(self) -> None:
        self.start = datetime.now()
        self.end: Optional[datetime] = None
        self.bytes = 0
        self.files = 0
        self.phase = ""
        self.phases: set = set()

    def update(self, st: dict) -> None:
        sc = parse_state_change(st)
        if sc and sc < self.start:
            self.start = sc
        phase = st.get("BackupPhase") or ""
        if phase:
            self.phase = phase
            self.phases.add(phase)
        prog = st.get("Progress")
        if isinstance(prog, dict) and "bytes" in prog:
            self.bytes = int(prog.get("bytes", 0))
            self.files = int(prog.get("files", 0))


def finalize(rec: Record, history: str) -> dict:
    rec.end = rec.end or datetime.now()
    row = {"start": rec.start.isoformat(timespec="seconds"),
           "end": rec.end.isoformat(timespec="seconds"),
           "bytes": rec.bytes, "files": rec.files, "last_phase": rec.phase}
    save_row(history, row)
    print(f"{datetime.now().isoformat(timespec='seconds')} recorded backup started "
          f"{row['start']}: {human(rec.bytes)}, {rec.files} files, "
          f"last phase {rec.phase}", flush=True)
    return row


def match_backup(row: dict, stamps: List[str]) -> str:
    """Name of the snapshot taken during this row's run, or ''."""
    lo = (datetime.fromisoformat(row["start"]) - timedelta(minutes=1)).strftime(STAMP)
    hi = (datetime.fromisoformat(row["end"]) + timedelta(minutes=2)).strftime(STAMP)
    hits = [s for s in stamps if lo <= s <= hi]
    return hits[-1] if hits else ""


def watch(history: str, poll: float, get=get_status) -> List[dict]:
    st = get()
    if not st or not st.get("Running"):
        return []
    rows = []
    rec = Record()
    while True:
        if st:
            if (rec.phases & LATE_PHASES) and st.get("BackupPhase") in EARLY_PHASES:
                # A new backup started without Running ever going false.
                rec.end = datetime.now()
                rows.append(finalize(rec, history))
                rec = Record()
            rec.update(st)
        time.sleep(poll)
        st = get()
        if st is not None and not st.get("Running"):
            break
    rows.append(finalize(rec, history))
    return rows


# ─── History ────────────────────────────────────────────────────────────────

def load_history(path: str) -> Dict[str, dict]:
    """Rows keyed by start time. Extra columns from older versions are ignored."""
    rows: Dict[str, dict] = {}
    if not os.path.exists(path):
        return rows
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows[r["start"]] = {"start": r["start"], "end": r["end"],
                                    "bytes": int(r["bytes"]), "files": int(r["files"]),
                                    "last_phase": r.get("last_phase", "")}
            except (KeyError, ValueError, TypeError):
                continue
    return rows


def save_row(path: str, row: dict) -> None:
    rows = load_history(path)
    rows[row["start"]] = row
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".history.")
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: r["start"]):
            w.writerow(r)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ─── Output ─────────────────────────────────────────────────────────────────

def human(n: int) -> str:
    for unit, div in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def print_table(rows: List[dict], stamps: Optional[List[str]], threshold: int) -> None:
    hdr = (f"{'Date':<14}  {'Start':<5}  {'End':<5}  {'Status':<10}  {'Files':>6}  "
           f"{'Copied':>10}  {'Took':>6}  {'Since prev':>10}  {'GB/hour':>7}")
    print(hdr)
    print("─" * len(hdr))
    for r in rows:
        took = (datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"]))
        took_s = f"{took.total_seconds() / 60:.0f} m"
        name = match_backup(r, stamps) if stamps is not None else ""
        if stamps is None:
            status = "unknown"
        elif name:
            status = "complete"
        elif r.get("last_phase") in LATE_PHASES:
            status = "deleted"
        else:
            status = "incomplete"
        hours = None
        if name:
            i = stamps.index(name)
            if i:
                hours = (datetime.strptime(stamps[i], STAMP)
                         - datetime.strptime(stamps[i - 1], STAMP)).total_seconds() / 3600
        since = f"{hours:.1f} h" if hours else "-"
        rate = f"{r['bytes'] / 1e9 / hours:.2f}" if hours else "-"
        flag = "  ◀ over threshold" if r["bytes"] > threshold else ""
        start = datetime.fromisoformat(r["start"])
        # A matched backup's name is when it finished; otherwise use when the watcher saw it stop.
        end = datetime.strptime(name, STAMP) if name else datetime.fromisoformat(r["end"])
        print(f"{start:%a %Y-%m-%d}  {start:%H:%M}  {end:%H:%M}  {status:<10}  {r['files']:>6}  "
              f"{human(r['bytes']):>10}  {took_s:>6}  {since:>10}  {rate:>7}{flag}")
    if stamps is not None and any(match_backup(r, stamps) == "" and r.get("last_phase") in LATE_PHASES
                                  for r in rows):
        print("\ndeleted = the backup finished, but Time Machine has since removed it.")
    if stamps is None:
        print(f"\nCan't read {TM_PLIST} (needs Full Disk Access), so backups can't be"
              "\nmatched to snapshots. Run this from a terminal that has it.")


# ─── LaunchAgent ────────────────────────────────────────────────────────────

def install_agent() -> None:
    script = os.path.realpath(__file__)
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(AGENT_PLIST), exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, script, "--watch"],
        "StartInterval": 60,
        "RunAtLoad": True,
        "StandardOutPath": AGENT_LOG,
        "StandardErrorPath": AGENT_LOG,
    }
    with open(AGENT_PLIST, "wb") as f:
        plistlib.dump(plist, f)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", domain, AGENT_PLIST],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    print(f"Installed {AGENT_PLIST}")
    print(f"  runs every minute: {sys.executable} {script} --watch")
    print(f"  history: {DEFAULT_HISTORY}")
    print(f"  log:     {AGENT_LOG}")
    print("  If you move the script or remove that Python, re-run --install.")


def uninstall_agent() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True)
    if os.path.exists(AGENT_PLIST):
        os.remove(AGENT_PLIST)
        print(f"Removed {AGENT_PLIST} (history kept at {DEFAULT_HISTORY})")
    else:
        print("LaunchAgent not installed.")


# ─── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(
        description="Report how much data each Time Machine backup copied.")
    ap.add_argument("--last", type=int, default=20, metavar="N",
                    help="show the last N recorded backups (default 20)")
    ap.add_argument("--threshold-gb", type=float, default=1.0,
                    help="flag backups larger than this (default 1.0)")
    ap.add_argument("--history", default=DEFAULT_HISTORY,
                    help=f"history CSV (default {DEFAULT_HISTORY})")
    ap.add_argument("--watch", action="store_true",
                    help="record the running backup, if any, then exit")
    ap.add_argument("--poll", type=float, default=5.0,
                    help="seconds between samples while watching (default 5)")
    ap.add_argument("--install", action="store_true", help="install the LaunchAgent")
    ap.add_argument("--uninstall", action="store_true", help="remove the LaunchAgent")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = ap.parse_args()

    if os.geteuid() == 0:
        sys.exit("Run as your normal user; this script doesn't need sudo.")

    if args.install:
        install_agent()
        return
    if args.uninstall:
        uninstall_agent()
        return
    if args.watch:
        watch(args.history, args.poll)
        return

    hist = load_history(args.history)
    if not hist:
        print(f"No backups recorded yet in {args.history}.")
        if not os.path.exists(AGENT_PLIST):
            print("Run tm-delta.py --install so the next backup gets recorded.")
        return
    rows = sorted(hist.values(), key=lambda r: r["start"], reverse=True)[:args.last]
    print_table(rows, snapshot_stamps(), int(args.threshold_gb * 1e9))
    if not os.path.exists(AGENT_PLIST):
        print("\nLaunchAgent not installed; new backups won't be recorded (--install).")


if __name__ == "__main__":
    main()
