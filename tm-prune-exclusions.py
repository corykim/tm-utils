#!/usr/bin/env python3
# tm-prune-exclusions.py (v1.11: Manual roots file shared with tm-exclude.sh, in ~/.config/tm-exclude)
# - Safe plist reading with plistlib
# - Filters to USER_HOME
# - Manual roots from file (default: ~/.config/tm-exclude/tm-exclusions.manual, shared with tm-exclude.sh)
# - Minimal-root computation with length sorting
# - Per-path tmutil removeexclusion without existence checks, using original paths
# - Verbose logs to stderr; final summary to stdout
# - Backups with timestamped filenames
# - Tool checks and sudo enforcement
# - Path normalization caching
# - Optional verification post-removal
# - Debug mode for raw plist contents and tmutil output

import argparse
import logging
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple

# Constants
VERSION = "1.11"
USER_PLIST_REL = "Library/Preferences/com.apple.TimeMachine.plist"
SYS_PLIST = "/Library/Preferences/com.apple.TimeMachine.plist"
DEFAULT_MANUAL_ROOTS_REL = ".config/tm-exclude/tm-exclusions.manual"

def setup_logging(verbose: bool, debug: bool, log_file: str = None) -> None:
    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.WARNING)
    if log_file:
        logging.basicConfig(filename=log_file, level=level, format='[%(levelname)s] %(message)s')
    else:
        logging.basicConfig(stream=sys.stderr, level=level, format='[%(levelname)s] %(message)s')

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prune redundant Time Machine exclusions.")
    parser.add_argument("--apply", action="store_true", help="Actually remove redundant exclusions (default: dry run)")
    parser.add_argument("--include-system", action="store_true", help="Consider/prune system plist entries (filtered to $HOME)")
    parser.add_argument("--all", action="store_true", help="mdfind xattr scan over all Spotlight (default: only $HOME)")
    parser.add_argument("--skip-xattr", action="store_true", help="Skip inheritable (xattr) exclusions entirely")
    parser.add_argument("--verbose", action="store_true", help="Log progress and full raw plist contents")
    parser.add_argument("--prune-system", action="store_true", help="Also remove redundant SYSTEM plist entries (requires sudo)")
    parser.add_argument("--backup", action="store_true", help="Backup plists before applying changes")
    parser.add_argument("--log-file", help="Redirect verbose logs to FILE instead of stderr")
    parser.add_argument("--verify", action="store_true", help="Verify removals by re-checking plists/xattrs")
    parser.add_argument("--debug", action="store_true", help="Log raw plist contents and tmutil output for debugging")
    parser.add_argument("--manual-roots-file", help=f"File containing manual exclusion paths, shared with tm-exclude.sh (default: ~/{DEFAULT_MANUAL_ROOTS_REL})")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args()

def resolve_home() -> str:
    if 'SUDO_USER' in os.environ and os.environ['SUDO_USER'] != 'root':
        return Path(f"~{os.environ['SUDO_USER']}").expanduser().as_posix()
    return os.path.expanduser("~")

def strip_comment(line: str) -> str:
    """Remove a comment and surrounding whitespace from one manual-roots line.

    A '#' starts a comment when it begins the line (after leading whitespace)
    or follows whitespace. A '#' inside a path, like '/a/b#c', is kept.
    """
    s = line.strip()
    if s.startswith('#'):
        return ''
    for i in range(1, len(s)):
        if s[i] == '#' and s[i - 1].isspace():
            return s[:i].strip()
    return s

def manual_roots_file() -> str:
    return args.manual_roots_file or os.path.join(resolve_home(), DEFAULT_MANUAL_ROOTS_REL)

def read_manual_roots(file_path: str) -> List[str]:
    try:
        with open(file_path, 'r') as f:
            paths = [p for p in (strip_comment(line) for line in f) if p]
        return paths
    except FileNotFoundError:
        logging.warning(f"Manual roots file not found: {file_path}")
        return []
    except Exception as e:
        logging.warning(f"Failed to read manual roots file {file_path}: {e}")
        return []

def normalize(path: str, cache: Dict[str, str]) -> str:
    if path in cache:
        return cache[path]
    # Not os.path.expanduser: under sudo, ~ must still mean the invoking user's home.
    np = resolve_home() + path[1:] if path == "~" or path.startswith("~/") else path
    np = np.rstrip('/') if np != '/' else np
    cache[path] = np
    return np

def is_descendant(child: str, parent: str) -> bool:
    if child == parent:
        return False
    if parent == '/':
        return child != '/'
    return child.startswith(parent + '/')

def read_plist_paths(plist_path: str, label: str, norm_cache: Dict[str, str]) -> List[Tuple[str, str]]:
    try:
        with open(plist_path, 'rb') as f:
            root = plistlib.load(f)
        if args.debug:
            logging.debug(f"Raw {label} plist contents: {root}")
        if isinstance(root, list):
            paths = [(s, normalize(s, norm_cache)) for s in root if isinstance(s, str)]
        elif isinstance(root, dict):
            skip_paths = root.get("SkipPaths", [])
            paths = [(s, normalize(s, norm_cache)) for s in skip_paths if isinstance(s, str)]
        else:
            paths = []
        return sorted(set(paths), key=lambda x: x[1])
    except Exception as e:
        logging.error(f"{label} plistlib.load failed for {plist_path}: {e}")
        return []

def filter_to_home(paths: List[Tuple[str, str]], home: str, norm_cache: Dict[str, str]) -> List[Tuple[str, str]]:
    home_norm = normalize(home, norm_cache)
    filtered = []
    for raw, norm in paths:
        if norm.startswith(home_norm + '/') or norm == home_norm:
            filtered.append((raw, norm))
    return sorted(set(filtered), key=lambda x: x[1])

def get_xattr_exclusions(scope: str, skip_xattr: bool, home: str, norm_cache: Dict[str, str]) -> List[Tuple[str, str]]:
    if skip_xattr:
        return []
    query = 'com_apple_backup_excludeItem = "*" && kMDItemKind = "Folder"'
    if scope != "home":
        query += ' && ! (kMDItemPath = "/System/*" || kMDItemPath = "/Library/*")'
    cmd = ['mdfind', query]
    if scope == "home":
        cmd.insert(1, '-onlyin')
        cmd.insert(2, home)
    try:
        raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip().split('\n')
        excl = [(p, normalize(p, norm_cache)) for p in raw if p]
        return sorted(set(excl), key=lambda x: x[1])
    except Exception as e:
        logging.warning(f"mdfind failed: {e}")
        return []

def prune_to_roots(paths: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    # Sort by normalized length for efficiency
    sorted_paths = sorted(paths, key=lambda x: len(x[1]))
    roots = []
    last = ""
    for raw, norm in sorted_paths:
        if not last or not is_descendant(norm, last):
            roots.append((raw, norm))
            last = norm
        else:
            logging.info(f"Covered by parent, skip root-candidate: {norm} (parent: {last})")
    return roots

def find_redundants(source: List[Tuple[str, str]], all_roots: List[Tuple[str, str]], section: str) -> List[Tuple[str, str]]:
    # Sort source by normalized length for efficiency
    sorted_source = sorted(source, key=lambda x: len(x[1]))
    to_remove = []
    last_non_child = ""
    for raw, norm in sorted_source:
        if last_non_child and is_descendant(norm, last_non_child):
            logging.info(f"{section} in-list redundant: {norm} (child of {last_non_child})")
            to_remove.append((raw, norm))
            continue
        last_non_child = norm
        covered = False
        for _, r_norm in all_roots:
            if norm == r_norm:
                continue
            if is_descendant(norm, r_norm):
                logging.info(f"{section} covered by root: {norm} (parent: {r_norm})")
                to_remove.append((raw, norm))
                covered = True
                break
        if covered:
            continue
    return to_remove

def backup_plists(apply: bool, backup: bool, user_plist: str, prune_system: bool, sys_plist: str) -> None:
    if apply and backup:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        try:
            shutil.copy(user_plist, f"{user_plist}.{timestamp}.bak")
            logging.info(f"Backed up USER plist to {user_plist}.{timestamp}.bak")
        except Exception as e:
            logging.warning(f"Failed to backup USER plist: {e}")
        if prune_system:
            try:
                shutil.copy(sys_plist, f"{sys_plist}.{timestamp}.bak")
                logging.info(f"Backed up SYSTEM plist to {sys_plist}.{timestamp}.bak")
            except Exception as e:
                logging.warning(f"Failed to backup SYSTEM plist: {e}")

def remove_one(mode: str, section: str, raw_path: str, apply: bool) -> None:
    # Log non-existent path but process it
    if not os.path.exists(raw_path):
        logging.info(f"{section} processing non-existent path: {raw_path}")
    cmd = ['tmutil', 'removeexclusion']
    if mode == 'p':
        cmd.append('-p')
    cmd.append(raw_path)
    cmd_str = ' '.join(cmd)
    logging.warning(f"Executing: {cmd_str}")
    if apply:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            logging.info(f"{section} removed: {raw_path}")
            if args.debug and result.stdout:
                logging.debug(f"{section} tmutil stdout: {result.stdout}")
            if args.debug and result.stderr:
                logging.debug(f"{section} tmutil stderr: {result.stderr}")
        except subprocess.CalledProcessError as e:
            logging.warning(f"{section} remove failed (rc={e.returncode}): {cmd_str}")
            if args.debug:
                logging.debug(f"{section} tmutil stdout: {e.stdout}")
                logging.debug(f"{section} tmutil stderr: {e.stderr}")
    else:
        logging.info(f"DRYRUN {section} would remove: {raw_path}")

def remove_paths(mode: str, section: str, paths: List[Tuple[str, str]], apply: bool) -> None:
    for raw, _ in paths:
        remove_one(mode, section, raw, apply)

def verify_removals(user_plist: str, sys_plist: str, home: str, skip_xattr: bool, include_system: bool, norm_cache: Dict[str, str]) -> None:
    logging.info("========= VERIFICATION: RE-CHECKING EXCLUSIONS =========")
    user_plist_all = read_plist_paths(user_plist, "USER", norm_cache) if os.path.exists(user_plist) else []
    sys_plist_all = read_plist_paths(sys_plist, "SYSTEM", norm_cache) if include_system and os.path.exists(sys_plist) else []
    user_skip = filter_to_home(user_plist_all, home, norm_cache)
    sys_skip = filter_to_home(sys_plist_all, home, norm_cache) if include_system else []
    xattr_excl = get_xattr_exclusions("all" if args.all else "home", skip_xattr, home, norm_cache)
    manual_roots = [(p, normalize(p, norm_cache)) for p in read_manual_roots(manual_roots_file())]
    manual_roots = sorted(set(manual_roots), key=lambda x: x[1])

    all_roots_raw = manual_roots + user_skip + xattr_excl
    if include_system:
        all_roots_raw += sys_skip
    all_roots = prune_to_roots(all_roots_raw)

    to_remove_user = find_redundants(user_skip, all_roots, "User")
    to_remove_xattr = find_redundants(xattr_excl, all_roots, "xattr") if not skip_xattr else []
    to_remove_system = find_redundants(sys_skip, all_roots, "system") if include_system and args.prune_system else []

    logging.info(f"Post-removal redundant USER paths: {len(to_remove_user)}")
    for _, p in to_remove_user:
        logging.info(f"  - {p}")
    if not skip_xattr:
        logging.info(f"Post-removal redundant XATTR paths: {len(to_remove_xattr)}")
        for _, p in to_remove_xattr:
            logging.info(f"  - {p}")
    if include_system and args.prune_system:
        logging.info(f"Post-removal redundant SYSTEM paths: {len(to_remove_system)}")
        for _, p in to_remove_system:
            logging.info(f"  - {p}")
    logging.info("=======================================================")

def main() -> None:
    global args
    args = parse_args()
    setup_logging(args.verbose, args.debug, args.log_file)

    # Tool checks
    for tool in ['tmutil', 'mdfind']:
        if shutil.which(tool) is None:
            logging.error(f"{tool} not found")
            sys.exit(1)

    home = resolve_home()
    user_plist = os.path.join(home, USER_PLIST_REL)

    # Check write access for plists
    if not os.access(user_plist, os.W_OK) and os.path.exists(user_plist):
        logging.error(f"No write access to USER plist: {user_plist}")
        sys.exit(1)
    if args.prune_system and not os.access(SYS_PLIST, os.W_OK):
        logging.error(f"Need sudo for --prune-system (cannot write to {SYS_PLIST})")
        sys.exit(1)

    norm_cache: Dict[str, str] = {}

    user_plist_all = read_plist_paths(user_plist, "USER", norm_cache) if os.path.exists(user_plist) else []
    sys_plist_all = read_plist_paths(SYS_PLIST, "SYSTEM", norm_cache) if args.include_system and os.path.exists(SYS_PLIST) else []

    if args.verbose:
        logging.info("========= RAW PLIST CONTENTS (no filtering) =========")
        logging.info(f"USER plist ({len(user_plist_all)} items):")
        for raw, _ in user_plist_all:
            logging.info(f"  - {raw}")
        if args.include_system:
            logging.info(f"SYSTEM plist ({len(sys_plist_all)} items):")
            for raw, _ in sys_plist_all:
                logging.info(f"  - {raw}")
        logging.info("=======================================================")

    user_skip = filter_to_home(user_plist_all, home, norm_cache)
    sys_skip = filter_to_home(sys_plist_all, home, norm_cache) if args.include_system else []

    xattr_excl = get_xattr_exclusions("all" if args.all else "home", args.skip_xattr, home, norm_cache)

    manual_roots = [(p, normalize(p, norm_cache)) for p in read_manual_roots(manual_roots_file())]
    manual_roots = sorted(set(manual_roots), key=lambda x: x[1])

    if args.verbose:
        logging.info("========= FILTERED INPUTS USED (under $HOME) =========")
        logging.info(f"USER filtered ({len(user_skip)}):")
        for _, p in user_skip:
            logging.info(f"  - {p}")
        if args.include_system:
            logging.info(f"SYSTEM filtered ({len(sys_skip)}):")
            for _, p in sys_skip:
                logging.info(f"  - {p}")
        if not args.skip_xattr:
            logging.info(f"XATTR ({len(xattr_excl)}):")
            for _, p in xattr_excl:
                logging.info(f"  - {p}")
        logging.info(f"MANUAL roots ({len(manual_roots)}):")
        for _, p in manual_roots:
            logging.info(f"  - {p}")
        logging.info("=======================================================")

    all_roots_raw = manual_roots + user_skip + xattr_excl
    if args.include_system:
        all_roots_raw += sys_skip
    all_roots = prune_to_roots(all_roots_raw)

    logging.info(f"Root count: {len(all_roots)}")
    if args.verbose:
        logging.info("----------------- PRUNED ROOTS USED -----------------")
        for _, r in all_roots:
            logging.info(f"  - {r}")
        logging.info("------------------------------------------------------")

    to_remove_user = find_redundants(user_skip, all_roots, "User")
    to_remove_xattr = find_redundants(xattr_excl, all_roots, "xattr") if not args.skip_xattr else []
    to_remove_system = find_redundants(sys_skip, all_roots, "system") if args.include_system and args.prune_system else []

    backup_plists(args.apply, args.backup, user_plist, args.prune_system, SYS_PLIST)

    if to_remove_user:
        logging.info(f"Removing {len(to_remove_user)} USER entries (one-by-one)…")
        remove_paths("p", "user", to_remove_user, args.apply)
        logging.info("User removals complete.")
    if to_remove_xattr:
        logging.info(f"Removing {len(to_remove_xattr)} XATTR entries (one-by-one)…")
        remove_paths("normal", "xattr", to_remove_xattr, args.apply)
        logging.info("xattr removals complete.")
    if to_remove_system:
        logging.info(f"Removing {len(to_remove_system)} SYSTEM entries (one-by-one)…")
        remove_paths("p", "system", to_remove_system, args.apply)
        logging.info("System removals complete.")

    if args.verify:
        verify_removals(user_plist, SYS_PLIST, home, args.skip_xattr, args.include_system, norm_cache)

    removed_total = len(to_remove_user) + len(to_remove_xattr) + len(to_remove_system)
    if args.apply:
        print(f"Done. Removed {removed_total} redundant exclusions.")
        if removed_total > 0:
            print("Consider restarting Time Machine service: 'sudo launchctl stop com.apple.backupd; sudo launchctl start com.apple.backupd'")
    else:
        print(f"Dry run complete. Would remove {removed_total} exclusions.")

if __name__ == "__main__":
    main()