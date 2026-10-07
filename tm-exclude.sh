#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# tm-exclude.sh
# Adds Time Machine exclusions for build artifacts, caches, and toolchain data.
#
# Usage:
#   ./tm-exclude.sh                          apply all exclusions
#   ./tm-exclude.sh --dry-run                show what would change, without applying
#   ./tm-exclude.sh --roots-file <path>      use a custom search roots file
#   ./tm-exclude.sh --roots-file=<path>      same
#   ./tm-exclude.sh --manual-file <path>     use a custom manual exclusions file
#   ./tm-exclude.sh --manual-file=<path>     same
#
# Search roots are read from:
#   ~/.config/tm-exclude/search-roots  (default)
# Override with --roots-file or the SEARCH_ROOTS_FILE environment variable.
#
# Manual exclusions (extra paths to exclude, files or folders) are read from:
#   ~/.config/tm-exclude/tm-exclusions.manual  (default; optional)
# Override with --manual-file or the MANUAL_FILE environment variable.
# tm-prune-exclusions.py reads the same file. One path per line; a leading ~
# means your home folder. '#' starts a comment at the start of a line or after
# whitespace; a '#' inside a path, like /a/b#c, is kept.
#
# Requirements:
#   - Run as your normal user, NOT with sudo. tmutil addexclusion and
#     removeexclusion -p require root, so the script plans every change as
#     your user, then applies them all in ONE sudo invocation (one prompt).
#     Nothing is changed if you decline that prompt.
#   - Terminal (or whichever app runs this) needs Full Disk Access so that
#     find can see protected folders.
#
# Where exclusions live:
#   Path-based (-p) exclusions are stored in the SkipPaths array of
#   /Library/Preferences/com.apple.TimeMachine.plist. tmutil has no verb to
#   list them, so this script reads that array with PlistBuddy.
#
# Deduplication:
#   SkipPaths is loaded once into memory. Each candidate is skipped if it is
#   already excluded or if any parent directory is excluded. The in-memory
#   list is updated after each successful add.
#
# Notes on *.pyc / *.pyo:
#   __pycache__ covers Python 3 bytecode. Stray Python 2 .pyc files are rare
#   enough to ignore.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

print_help() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

# ─── CLI ─────────────────────────────────────────────────────────────────────

DRY_RUN=false
SEARCH_ROOTS_FILE="${SEARCH_ROOTS_FILE:-$HOME/.config/tm-exclude/search-roots}"
MANUAL_FILE="${MANUAL_FILE:-$HOME/.config/tm-exclude/tm-exclusions.manual}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) print_help; exit 0 ;;
        --roots-file)
            [[ $# -ge 2 ]] || { echo "Error: --roots-file requires a path" >&2; exit 1; }
            SEARCH_ROOTS_FILE="$2"; shift 2 ;;
        --roots-file=*) SEARCH_ROOTS_FILE="${1#--roots-file=}"; shift ;;
        --manual-file)
            [[ $# -ge 2 ]] || { echo "Error: --manual-file requires a path" >&2; exit 1; }
            MANUAL_FILE="$2"; shift 2 ;;
        --manual-file=*) MANUAL_FILE="${1#--manual-file=}"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done
SEARCH_ROOTS_FILE="${SEARCH_ROOTS_FILE/#\~/$HOME}"
MANUAL_FILE="${MANUAL_FILE/#\~/$HOME}"

if [[ $EUID -eq 0 ]]; then
    echo "Error: run this as your normal user, not with sudo." >&2
    echo "The script calls sudo itself where needed, and \$HOME must be yours." >&2
    exit 1
fi

# ─── Configuration ───────────────────────────────────────────────────────────

TM_PLIST="/Library/Preferences/com.apple.TimeMachine.plist"
PLISTBUDDY="/usr/libexec/PlistBuddy"

# Maximum depth to search within each search root.
# 6 covers deep monorepo workspace layouts, e.g.:
#   root/client/repo/packages/@scope/pkg-a/node_modules  (depth 6)
FIND_DEPTH=6

# Directory names to locate and exclude under search roots.
#
# WARNING: 'dist', 'build', 'out', and 'target' are generic names. Run with
# --dry-run first and review the output, particularly if you have
# non-artifact directories with these names.
PROJECT_PATTERNS=(
    "node_modules"     # npm / yarn / pnpm
    ".next"            # Next.js build cache
    ".nuxt"            # Nuxt.js build cache
    ".gradle"          # Gradle build cache (per-project)
    "__pycache__"      # Python 3 bytecode
    ".terraform"       # Terraform provider plugin cache
    "dist"             # Compiled output (JS, Rust, etc.)
    "build"            # Compiled output (many ecosystems)
    "out"              # Compiled output (Next.js static, etc.)
    "target"           # Compiled output (Rust / Maven / Scala)
)

# Fixed, well-known paths. Entries that don't exist on disk are skipped.
# Globs expand when this array is assigned; a glob with no match stays
# literal and is reported as [not found].
FIXED_PATHS=(
    # ── Xcode ──────────────────────────────────────────────────────────────
    "$HOME/Library/Developer/Xcode/DerivedData"

    # ── npm / Yarn / uv ────────────────────────────────────────────────────
    "$HOME/.npm"
    "$HOME/.yarn/cache"
    "$HOME/.cache/yarn"
    "$HOME/.cache/uv"

    # ── Maven / Gradle ─────────────────────────────────────────────────────
    "$HOME/.m2/repository"
    "$HOME/.gradle/caches"

    # ── Rust / Cargo ───────────────────────────────────────────────────────
    # ~/.cargo/bin is intentionally not excluded; installed binaries are worth keeping.
    "$HOME/.cargo/registry"
    "$HOME/.rustup/toolchains"

    # ── Language version managers ──────────────────────────────────────────
    "$HOME/.pyenv/versions"
    "$HOME/.asdf/installs"
    # Whole ~/.nvm: Node builds plus nvm's own scripts and aliases,
    # all restorable by reinstalling nvm.
    "$HOME/.nvm"
    "$HOME/.rbenv/versions"

    # ── Java (user-installed JDKs via Toolbox / SDKMAN / manual) ──────────
    "$HOME/Library/Java/JavaVirtualMachines"

    # ── Terraform (global plugin cache) ────────────────────────────────────
    # .terraform.lock.hcl and terraform.tfstate are NOT excluded.
    "$HOME/.terraform.d/plugin-cache"

    # ── Kubernetes / AWS CLI ───────────────────────────────────────────────
    "$HOME/.kube/cache"
    "$HOME/.aws/cli/cache"

    # ── Docker Desktop VM disk ─────────────────────────────────────────────
    "$HOME/Library/Containers/com.docker.docker/Data/vms"

    # -- Apple container: per-container disk images --------------------------
    # Each container's rootfs.ext4 is a sparse image (hundreds of GB apparent,
    # a few GB on disk) that is rewritten while the container runs. Container
    # state is lost on restore; images and layers (content/, snapshots/) are
    # kept, since they rarely change.
    "$HOME/Library/Application Support/com.apple.container/containers"

    # ── pnpm global content store ──────────────────────────────────────────
    "$HOME/Library/pnpm"

    # ── macOS derived data (rebuilt by system / Spotlight) ─────────────────
    "/.Spotlight-V100"
    "$HOME/Library/Metadata/CoreSpotlight"
    "$HOME/Library/IdentityServices"
    "$HOME/Library/Biome"
    "$HOME/Library/HTTPStorages"
    "$HOME/Library/WebKit"
    "$HOME/Library/IntelligencePlatform"
    "$HOME/Library/Containers/com.apple.wallpaper.agent"

    # ── App Support caches ─────────────────────────────────────────────────
    "$HOME/Library/Application Support/Caches"
    "$HOME/Library/Application Support/pyinstaller"

    # ── Updater caches (re-downloadable) ───────────────────────────────────
    "$HOME/Library/Application Support/Google/GoogleUpdater"
    "$HOME/Library/Application Support/krisp/update"
    "/Library/Application Support/Microsoft/EdgeUpdater"

    # ── Krisp: downloaded AI noise-cancellation models and Electron partitions
    "$HOME/Library/Application Support/krisp/models"
    "$HOME/Library/Application Support/krisp/Partitions"

    # ── Zoom: downloaded speech recognition model and bundled Chromium plugin
    "$HOME/Library/Application Support/zoom.us/asr"
    "$HOME/Library/Application Support/zoom.us/CefPlugin"

    # ── Chrome Canary app bundle (profile data lives in ~/Library) ─────────
    "/Applications/Google Chrome Canary.app"

    # ── Chrome per-profile caches (Service Worker, HTTP cache, V8 code cache)
    "$HOME/Library/Application Support/Google/Chrome"/*/"Service Worker/CacheStorage"
    "$HOME/Library/Application Support/Google/Chrome"/*/"Cache"
    "$HOME/Library/Application Support/Google/Chrome"/*/"Code Cache"

    # ── System caches (rebuilt automatically by macOS) ─────────────────────
    "/System/Library/Caches"

    # ── Corporate endpoint agents (Tanium, Defender) ───────────────────────
    # Large, constantly rewritten, and redeployed by IT/MDM. A restored
    # machine may show these agents as unhealthy until IT reinstalls them.
    "/Library/Tanium"
    "/Library/Application Support/Microsoft/Defender"

    # ── Electron app caches: Teams and Slack ───────────────────────────────
    # Teams: only Caches; Application Support/Microsoft/MSTeams has local data.
    "$HOME/Library/Containers/com.microsoft.teams2/Data/Library/Caches"
    # Slack: Service Worker and Cache are the large items (~910 MB).
    # IndexedDB and Local Storage are kept (small, contains app state).
    "$HOME/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack/Service Worker"
    "$HOME/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack/Cache"

    # ── Microsoft Office 365 Group Container ───────────────────────────────
    # SolutionPackages: downloaded Office add-in bundles; re-downloaded by Office.
    # FontCache: rebuilt by Office on first use.
    # (Outlook profiles and OneDrive files are kept.)
    "$HOME/Library/Group Containers/UBF8T346G9.Office/SolutionPackages"
    "$HOME/Library/Group Containers/UBF8T346G9.Office/FontCache"
    "$HOME/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/FileProviderLogs"
    "$HOME/Library/Group Containers/UBF8T346G9.com.microsoft.teams/Library/Application Support/Logs"

    # ── Claude Desktop / Claude Code ───────────────────────────────────────
    # claude-code: Claude Code app files bundled inside the desktop app.
    # claude-code-vm: versioned VM runtime downloaded by Claude Code.
    # vm_bundles: Linux VM disk images (10+ GB), re-downloaded as needed.
    # Cache / Code Cache / GPUCache: standard Electron/V8/GPU caches.
    # Sessions (local-agent-mode-sessions, claude-code-sessions) are kept.
    "$HOME/Library/Application Support/Claude/claude-code"
    "$HOME/Library/Application Support/Claude-Home/claude-code"
    "$HOME/Library/Application Support/Claude-Home/claude-code-vm"
    "$HOME/Library/Application Support/Claude-Home/vm_bundles"
    "$HOME/Library/Application Support/Claude-Home/Cache"
    "$HOME/Library/Application Support/Claude-Home/Code Cache"
    "$HOME/Library/Application Support/Claude-Home/GPUCache"

    # ── Utilities and Productivity ───────────────────────────────────────────
    "$HOME/Library/Application Support/Notion/Partitions"
    "$HOME/Library/Application Support/Superhuman"
)

# ─── Load search roots ───────────────────────────────────────────────────────

SEARCH_ROOTS=()

load_search_roots() {
    local file="$1" line
    if [[ ! -f "$file" ]]; then
        printf 'Error: search roots file not found: %s\n\n' "$file" >&2
        printf 'Create it with one directory path per line, e.g.:\n  ~/projects\n  ~/work\n\n' >&2
        printf 'Default location: ~/.config/tm-exclude/search-roots\n' >&2
        exit 1
    fi

    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        # Expand a leading ~ without eval, so file content is never executed.
        line="${line/#\~/$HOME}"
        SEARCH_ROOTS+=("$line")
    done < "$file"

    if [[ ${#SEARCH_ROOTS[@]} -eq 0 ]]; then
        printf 'Warning: search roots file is empty or contains only comments: %s\n' "$file" >&2
    fi
}

load_search_roots "$SEARCH_ROOTS_FILE"

# --- Load manual exclusions ---
# Comment rules must match strip_comment() in tm-prune-exclusions.py, which
# reads the same file: '#' starts a comment only at the start of a line or
# after whitespace.

MANUAL_PATHS=()
MANUAL_FOUND=false

load_manual_paths() {
    local file="$1" line
    [[ -f "$file" ]] || return 0
    MANUAL_FOUND=true
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line#"${line%%[![:space:]]*}"}"
        [[ "$line" == \#* ]] && continue
        line="${line%%[[:space:]]#*}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        # Expand a leading ~ without eval, so file content is never executed.
        line="${line/#\~/$HOME}"
        MANUAL_PATHS+=("$line")
    done < "$file"
}

load_manual_paths "$MANUAL_FILE"

# ─── Plist access ────────────────────────────────────────────────────────────
# Normally world-readable, so no sudo is needed to read it. If it isn't,
# reading costs one extra sudo prompt.

NEED_SUDO_READ=false
if [[ ! -r "$TM_PLIST" ]]; then
    NEED_SUDO_READ=true
    echo "Note: $TM_PLIST is not readable; using sudo to read it (extra prompt)."
fi

# ─── Read current path exclusions (SkipPaths) ────────────────────────────────

read_skip_paths() {
    # PlistBuddy prints an array as:
    #   Array {
    #       /some/path
    #   }
    # Drop the first and last lines, strip indentation, expand any leading ~,
    # and strip trailing slashes. A missing SkipPaths key yields nothing.
    local -a cmd=("$PLISTBUDDY" -c "Print :SkipPaths" "$TM_PLIST")
    $NEED_SUDO_READ && cmd=(sudo "${cmd[@]}")
    { "${cmd[@]}" 2>/dev/null || true; } \
        | sed -e '1d' -e '$d' -e 's/^[[:space:]]*//' \
        | while IFS= read -r p; do
              [[ -z "$p" ]] && continue
              p="${p/#\~/$HOME}"
              [[ "$p" != "/" ]] && p="${p%/}"
              printf '%s\n' "$p"
          done
}

INITIAL_EXCLUSIONS="$(read_skip_paths)"
CURRENT_EXCLUSIONS="$INITIAL_EXCLUSIONS"

# ─── Derived: combined find -name args ───────────────────────────────────────

FIND_NAME_ARGS=()
for _pattern in "${PROJECT_PATTERNS[@]}"; do
    [[ ${#FIND_NAME_ARGS[@]} -gt 0 ]] && FIND_NAME_ARGS+=(-o)
    FIND_NAME_ARGS+=(-name "$_pattern")
done
unset _pattern

# ─── State ───────────────────────────────────────────────────────────────────

ADDED=0
SKIPPED_DUPLICATE=0
SKIPPED_COVERED=0
SKIPPED_MISSING=0
REMOVED=0
FAILED=0
COVERING=""
TO_ADD=()
TO_REMOVE=()

# ─── Helpers ─────────────────────────────────────────────────────────────────

is_excluded() {
    # Exact whole-line fixed-string match.
    [[ -n "$CURRENT_EXCLUSIONS" ]] || return 1
    printf '%s\n' "$CURRENT_EXCLUSIONS" | grep -qxF -- "$1"
}

covered_by_parent() {
    # True if any ancestor directory of $1 is already excluded.
    local p="$1"
    while [[ "$p" == /*/* ]]; do
        p="${p%/*}"
        if is_excluded "$p"; then COVERING="$p"; return 0; fi
    done
    return 1
}

add_exclusion() {
    local path="$1"
    [[ "$path" != "/" ]] && path="${path%/}"

    if [[ ! -e "$path" ]]; then
        printf '  SKIP  %-14s  %s\n' "[not found]" "$path"
        SKIPPED_MISSING=$((SKIPPED_MISSING + 1))
        return
    fi

    if is_excluded "$path"; then
        printf '  SKIP  %-14s  %s\n' "[duplicate]" "$path"
        SKIPPED_DUPLICATE=$((SKIPPED_DUPLICATE + 1))
        return
    fi

    if covered_by_parent "$path"; then
        printf '  SKIP  %-14s  %s  (under %s)\n' "[covered]" "$path" "$COVERING"
        SKIPPED_COVERED=$((SKIPPED_COVERED + 1))
        return
    fi

    if $DRY_RUN; then
        printf '  DRY   %-14s  %s\n' "" "$path"
        ADDED=$((ADDED + 1))
    else
        printf '  QUEUE %-14s  %s\n' "[add]" "$path"
        TO_ADD+=("$path")
    fi
    # Record in memory so later candidates (e.g. children of this path)
    # are judged as they would be once the add is applied.
    CURRENT_EXCLUSIONS="${CURRENT_EXCLUSIONS:+$CURRENT_EXCLUSIONS$'\n'}$path"
}

section() { printf '\n━━━ %s\n' "$1"; }

# ─── Main ────────────────────────────────────────────────────────────────────

$DRY_RUN && printf 'DRY RUN: no changes will be made\n'
printf 'Existing path exclusions loaded: %d\n' \
    "$( [[ -n "$INITIAL_EXCLUSIONS" ]] && printf '%s\n' "$INITIAL_EXCLUSIONS" | wc -l | tr -d ' ' || echo 0 )"

section "Fixed paths"
for path in "${FIXED_PATHS[@]}"; do
    add_exclusion "$path"
done

section "Manual paths"
printf '  File: %s\n' "$MANUAL_FILE"
if ! $MANUAL_FOUND; then
    printf '  (file not found; optional)\n'
elif [[ ${#MANUAL_PATHS[@]} -eq 0 ]]; then
    printf '  (file has no paths)\n'
fi
for path in "${MANUAL_PATHS[@]+"${MANUAL_PATHS[@]}"}"; do
    if [[ "$path" != /* ]]; then
        printf '  SKIP  %-14s  %s\n' "[not absolute]" "$path"
        SKIPPED_MISSING=$((SKIPPED_MISSING + 1))
        continue
    fi
    add_exclusion "$path"
done

section "Project-level patterns  (depth ≤ $FIND_DEPTH)"
printf '  Roots file: %s\n' "$SEARCH_ROOTS_FILE"
printf '  Patterns:   %s\n' "${PROJECT_PATTERNS[*]}"
printf '  Roots:      %s\n\n' "${SEARCH_ROOTS[*]:-}"

for root in "${SEARCH_ROOTS[@]+"${SEARCH_ROOTS[@]}"}"; do
    if [[ ! -d "$root" ]]; then
        printf '  ── %s  (not a directory, skipped)\n' "$root"
        continue
    fi
    printf '  ── %s\n' "$root"
    root_found=0
    while IFS= read -r -d '' dir; do
        add_exclusion "$dir"
        root_found=$((root_found + 1))
    done < <(
        find "$root" \
            -maxdepth "$FIND_DEPTH" \
            -type d \
            \( "${FIND_NAME_ARGS[@]}" \) \
            -prune -print0 \
            2>/dev/null || true
    )
    [[ $root_found -eq 0 ]] && printf '    (none found)\n'
done

section "Removing stale exclusions  (under \$HOME only)"
# Iterates over the snapshot taken before this run. Only paths under $HOME
# are considered; system-managed entries are not ours to remove.
while IFS= read -r stale_path; do
    [[ -z "$stale_path" ]] && continue
    [[ "$stale_path" == "$HOME/"* ]] || continue
    [[ -e "$stale_path" ]] && continue
    if $DRY_RUN; then
        printf '  DRY   %-14s  %s\n' "[stale]" "$stale_path"
        REMOVED=$((REMOVED + 1))
    else
        printf '  QUEUE %-14s  %s\n' "[remove]" "$stale_path"
        TO_REMOVE+=("$stale_path")
    fi
done <<< "$INITIAL_EXCLUSIONS"
if $DRY_RUN; then
    [[ $REMOVED -eq 0 ]] && printf '  (none)\n'
else
    [[ ${#TO_REMOVE[@]} -eq 0 ]] && printf '  (none)\n'
fi

# ─── Apply: one sudo invocation for all changes ──────────────────────────────
# Paths are passed as arguments (never interpolated into the script text),
# so spaces and special characters are safe. Every path is absolute, so the
# '::remove::' separator can't collide with one.
#
# Exit code from the root script: 0 = all succeeded, 100+N = N failures
# (capped). Anything else means sudo itself was declined or failed.

ROOT_SCRIPT='
fail=0; mode=add
for p in "$@"; do
    if [[ "$p" == "::remove::" ]]; then mode=remove; continue; fi
    if [[ "$mode" == add ]]; then
        if tmutil addexclusion -p "$p"; then
            printf "  ADD    %-13s  %s\n" "" "$p"
        else
            printf "  FAIL   %-13s  %s\n" "[add]" "$p" >&2; fail=$((fail + 1))
        fi
    else
        if tmutil removeexclusion -p "$p"; then
            printf "  REMOVE %-13s  %s\n" "" "$p"
        else
            printf "  FAIL   %-13s  %s\n" "[remove]" "$p" >&2; fail=$((fail + 1))
        fi
    fi
done
[[ $fail -eq 0 ]] && exit 0
exit $(( fail > 27 ? 127 : 100 + fail ))
'

if ! $DRY_RUN; then
    section "Applying changes  (${#TO_ADD[@]} add, ${#TO_REMOVE[@]} remove)"
    if [[ ${#TO_ADD[@]} -eq 0 && ${#TO_REMOVE[@]} -eq 0 ]]; then
        printf '  Nothing to do; sudo not needed.\n'
    else
        set +e
        sudo /bin/bash -c "$ROOT_SCRIPT" tm-exclude-root \
            ${TO_ADD[@]+"${TO_ADD[@]}"} \
            ::remove:: \
            ${TO_REMOVE[@]+"${TO_REMOVE[@]}"}
        rc=$?
        set -e
        total=$(( ${#TO_ADD[@]} + ${#TO_REMOVE[@]} ))
        if [[ $rc -eq 0 ]]; then
            ADDED=${#TO_ADD[@]}
            REMOVED=${#TO_REMOVE[@]}
        elif [[ $rc -ge 100 ]]; then
            FAILED=$(( rc - 100 ))
            printf '  %d of %d operations failed (see FAIL lines above).\n' "$FAILED" "$total" >&2
            printf '  Re-run to retry; existing exclusions are skipped as duplicates.\n' >&2
            # Added/Removed below are attempted counts; FAIL lines show which failed.
            ADDED=${#TO_ADD[@]}
            REMOVED=${#TO_REMOVE[@]}
        else
            printf '  sudo was declined or failed (rc=%d). No changes were made.\n' "$rc" >&2
            FAILED=$total
        fi
    fi
fi

section "Summary"
$DRY_RUN && printf '  (dry run: no changes made)\n'
printf '  Added:       %d\n' "$ADDED"
printf '  Removed:     %d\n' "$REMOVED"
printf '  Duplicates:  %d\n' "$SKIPPED_DUPLICATE"
printf '  Covered:     %d\n' "$SKIPPED_COVERED"
printf '  Not found:   %d\n' "$SKIPPED_MISSING"
printf '  Failed:      %d\n' "$FAILED"
printf '\n'

[[ $FAILED -eq 0 ]]