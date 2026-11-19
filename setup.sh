#!/usr/bin/env bash
# setup.sh — install or uninstall `claude-auto`.
#
# One-liner install:
#   curl -fsSL https://raw.githubusercontent.com/yukkes/claude-auto/main/setup.sh | bash
#
# One-liner uninstall:
#   curl -fsSL https://raw.githubusercontent.com/yukkes/claude-auto/main/setup.sh | bash -s -- --uninstall
#
# Local usage:
#   ./setup.sh                 # installs ~/.local/bin/claude-auto
#   ./setup.sh --bin-dir DIR   # install into custom directory
#   ./setup.sh --uninstall     # remove claude-auto
#
# Does NOT replace `claude` and does NOT touch .bashrc.
set -euo pipefail

BIN_NAME="claude-auto"
DRIVER="claude_auto.py"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/claude-auto"
RAW_BASE="${CLAUDE_AUTO_RAW_URL:-https://raw.githubusercontent.com/yukkes/claude-auto/main}"
RC="${BASHRC:-$HOME/.bashrc}"
LEGACY_BEGIN="# >>> cc-autoapprove >>>"
LEGACY_END="# <<< cc-autoapprove <<<"

die() { echo "error: $*" >&2; exit 1; }
usage() {
  cat <<'EOF'
Usage:
  curl -fsSL https://raw.githubusercontent.com/yukkes/claude-auto/main/setup.sh | bash
  curl -fsSL https://raw.githubusercontent.com/yukkes/claude-auto/main/setup.sh | bash -s -- --uninstall
  ./setup.sh [--bin-dir DIR] [--uninstall]
EOF
}

BIN_DIR=""
ACTION="install"
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) ACTION="uninstall" ;;
    --bin-dir)   BIN_DIR="${2:-}"; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

command -v python3 >/dev/null || die "python3 not found"

# Real path of the claude binary (override with CLAUDE_REAL=...)
CLAUDE_ENTRY="${CLAUDE_REAL:-$(command -v claude || true)}"
[ -n "$CLAUDE_ENTRY" ] || die "claude not found on PATH"
CLAUDE_REAL=$(readlink -f "$CLAUDE_ENTRY")

# Default target: the directory of the claude on PATH (usually ~/.local/bin).
# The real binary may live under versions/, so use the pre-readlink path.
[ -n "$BIN_DIR" ] || BIN_DIR=$(dirname "$CLAUDE_ENTRY")
TARGET="$BIN_DIR/$BIN_NAME"

# Remove the shell-function block that older versions wrote into .bashrc
strip_legacy() {
  [ -f "$RC" ] || return 0
  grep -qF "$LEGACY_BEGIN" "$RC" || return 0
  python3 - "$RC" "$LEGACY_BEGIN" "$LEGACY_END" <<'PY'
import sys
path, begin, end = sys.argv[1:4]
out, skip = [], False
for l in open(path).read().splitlines(True):
    if l.strip() == begin:
        skip = True
        continue
    if l.strip() == end:
        skip = False
        continue
    if not skip:
        out.append(l)
open(path, "w").writelines(out)
PY
  echo "removed legacy .bashrc block: $RC"
}

if [ "$ACTION" = "uninstall" ]; then
  rm -f "$TARGET" && echo "removed: $TARGET"
  rm -rf "$DATA_DIR" && echo "removed: $DATA_DIR"
  strip_legacy
  exit 0
fi

# Install the driver into ~/.local/share/claude-auto/claude_auto.py
# 1. local checkout: claude_auto.py sits next to this script -> copy it
# 2. otherwise download it from the repo (keeps driver and installer in sync)
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
LOCAL=""
if [ -n "$SCRIPT_SOURCE" ] && [ -f "$SCRIPT_SOURCE" ]; then
  CANDIDATE="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)/$DRIVER"
  [ -f "$CANDIDATE" ] && LOCAL="$CANDIDATE"
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
if [ -n "$LOCAL" ]; then
  cp "$LOCAL" "$STAGE/$DRIVER"
  echo "driver: copied from $LOCAL"
else
  command -v curl >/dev/null || die "curl required to download the driver"
  curl -fsSL "${RAW_BASE%/}/$DRIVER" -o "$STAGE/$DRIVER" \
    || die "failed to download ${RAW_BASE%/}/$DRIVER"
  echo "driver: downloaded from ${RAW_BASE%/}/$DRIVER"
fi
python3 -m py_compile "$STAGE/$DRIVER" || die "driver failed to compile"

mkdir -p "$DATA_DIR"
# replace; drop the old single-file driver and the package layout
rm -rf "$DATA_DIR/claude_auto" "$DATA_DIR/cc-autoapprove.py" "$DATA_DIR/__pycache__"
cp "$STAGE/$DRIVER" "$DATA_DIR/$DRIVER"

# Install the wrapper
mkdir -p "$BIN_DIR"
cat > "$TARGET" <<EOF
#!/usr/bin/env bash
# $BIN_NAME — runs claude on an internal pty with automatic prompt approval
# binary: $CLAUDE_REAL
# remove: setup.sh --uninstall
REAL="\${CLAUDE_REAL:-$CLAUDE_REAL}"
DRIVER="\${CLAUDE_AUTO_DRIVER:-$DATA_DIR/$DRIVER}"
# Driver missing -> behave like plain claude rather than fail.
[ -f "\$DRIVER" ] || exec "\$REAL" "\$@"
# -I: ignore PYTHONPATH / user site, and leave the child's environment alone.
exec env CLAUDE_BIN="\$REAL" python3 -I "\$DRIVER" "\$@"
EOF
chmod +x "$TARGET"

strip_legacy

echo "installed: $TARGET"
echo "  claude:  $CLAUDE_REAL"
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) echo "warning: $BIN_DIR is not on your PATH" >&2 ;;
esac
echo "usage: $BIN_NAME [same arguments as claude]"
