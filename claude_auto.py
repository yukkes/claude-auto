#!/usr/bin/env python3
"""claude-auto — auto-approve Claude Code permission prompts.

Signal source: the session transcript
(~/.claude/projects/<cwd-slug>/<session>.jsonl), i.e. the actual API message
log (assistant tool_use / tool_result). It never reads TUI text, so terminal
size, ANSI wrapping and UI locale are irrelevant.

Rule:
  a tool_use exists and its tool_result has not arrived yet  -> awaiting approval
  => write Enter (= confirm:yes) to the pty master; once the result lands,
     move on.

The TUI runs on a pty we own (pty.fork), so no tmux and no screen scraping are
involved: we relay bytes both ways and inject keys straight into the master fd.
"""
import contextlib
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import tempfile
import termios
import time
import tty
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

__version__ = "0.2.0"


# ============================================================================
# Settings from the environment, and Claude Code behaviour mirrored here
# ============================================================================

def truthy(value):
    return value not in (None, "", "0", "false")


# ---- user settings (environment) ------------------------------------------
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
MODE = os.environ.get("CC_MODE", "")
DEBUG = os.environ.get("CC_DEBUG") == "1"
LOG_FILE = os.environ.get("CC_LOG_FILE", "")
COLS = int(os.environ.get("CC_COLS", "200"))
ROWS = int(os.environ.get("CC_ROWS", "50"))
IDLE_GRACE = float(os.environ.get("CC_IDLE_GRACE", "1.0"))
TRUST_ANYWAY = os.environ.get("CC_TRUST_ANYWAY") == "1"
NO_WRAP = truthy(os.environ.get("CC_NO_WRAP"))
POLL = float(os.environ.get("CC_POLL", "0.05"))      # transcript poll / select timeout (s)

# ---- approval timing --------------------------------------------------------
RETRY = 0.15         # re-send Enter for the same tool_use every (s)
MAX_TRIES = 40       # max Enter sends per tool_use
UP_AFTER = 20        # from this attempt on, prefix Up (move to the approve row)
EXIT_WAIT = 5.0      # how long to wait for claude to quit before SIGKILL
DRAIN_WAIT = 2.0     # how long to keep draining output after the child exits
# The transcript cannot tell "prompt on screen" from "auto-allowed tool still
# running". A running tool repaints its spinner (~10 writes/s), a waiting prompt
# barely repaints (~2 writes/s), so retries happen only while the TUI is quiet.
QUIET_WINDOW = 1.0   # retries only when the TUI wrote at most QUIET_MAX_WRITES
QUIET_MAX_WRITES = 3 #   chunks within the last QUIET_WINDOW seconds

KEY_ENTER, KEY_UP, EXIT_CMD = b"\r", b"\x1b[A", b"/exit\r"

# ---- Claude Code behaviour mirrored here (verified against v2.1.327) --------
SLUG_MAX = 200                                     # project dir names: hash past this
RESERVED_NAME = re.compile(r"con|prn|aux|nul|com[0-9]|lpt[0-9]", re.I)
INTERACTIVE_TOOLS = {"AskUserQuestion"}            # left to the user when a tty is attached
RESUME_FLAGS = {"-c", "--continue", "--from-pr", "--teleport"}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# First arguments that run plain claude: non-interactive or management commands.
PASS_THROUGH = {"-p", "--print", "--version", "-v", "--help", "-h", "help", "update",
                "doctor", "config", "mcp", "install", "setup-token"}


# ============================================================================
# Logging (never onto the TUI)
# ============================================================================

def _log_target():
    if LOG_FILE:
        return LOG_FILE
    if os.isatty(1):                  # never scribble on the TUI
        return os.path.join(tempfile.gettempdir(), f"claude-auto-{os.getpid()}.log")
    return None                       # stderr keeps stdout clean


LOG_TARGET = _log_target()


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] [autoapprove] {msg}"
    with contextlib.suppress(OSError):
        if LOG_TARGET:
            with open(LOG_TARGET, "a") as f:
                f.write(line + "\n")
            return
    with contextlib.suppress(OSError):
        print(line, file=sys.stderr, flush=True)


# ============================================================================
# Where Claude Code keeps things, and whether a directory is trusted
# ============================================================================

def _utf16_units(s):
    b = s.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def js_hash36(s):
    """Math.abs(hashCode(s)).toString(36), hashCode being Java-style int32 over UTF-16."""
    h = 0
    for u in _utf16_units(s):
        h = ((h << 5) - h + u) & 0xFFFFFFFF
    n = abs(h - 0x100000000 if h >= 0x80000000 else h)
    digits, out = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def project_slug(path):
    """Mirror Claude Code: path.replace(/[^a-zA-Z0-9]/g, "-"), hashed past 200 chars.

    The JS regex works on UTF-16 code units, so a non-BMP character becomes "--".
    """
    slug = "".join(
        chr(u) if (0x30 <= u <= 0x39 or 0x41 <= u <= 0x5A or 0x61 <= u <= 0x7A) else "-"
        for u in _utf16_units(path))
    if len(slug) <= SLUG_MAX:
        return slug
    return f"{slug[:SLUG_MAX]}-{js_hash36(path)}"


def config_dir(env):
    return unicodedata.normalize("NFC", env.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))


def project_dir(cwd, env):
    """~/.claude/projects/<slug>; CLAUDE_CODE_PROJECT_DIR_NAME overrides the slug,
    but only with CLAUDE_CONFIG_DIR set."""
    name = env.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if not (env.get("CLAUDE_CONFIG_DIR") and name
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) and not RESERVED_NAME.fullmatch(name)):
        name = project_slug(cwd)
    return os.path.join(config_dir(env), "projects", name)


def global_config_path(env):
    """The global config holding `projects`: a non-empty legacy
    <config dir>/.config.json wins, else $CLAUDE_CONFIG_DIR (or ~)/.claude<suffix>.json."""
    legacy = os.path.join(config_dir(env), ".config.json")
    with contextlib.suppress(OSError):
        if os.path.isfile(legacy) and os.path.getsize(legacy) > 0:
            return legacy
    suffix = "-custom-oauth" if env.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""
    return os.path.join(env.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~"), f".claude{suffix}.json")


def workspace_trusted(cwd, env):
    """Persisted trust for cwd: True / False / None (config unreadable).

    projects[<dir>].hasTrustDialogAccepted on cwd or any ancestor counts, but
    inside a git repo (nearest dir holding a non-symlink .git) only up to the
    repo root, whose own key also counts. The home folder's trust is
    session-only and never persisted. There is no CLI flag to pre-accept it;
    CLAUDE_CODE_SANDBOXED skips it.
    """
    if truthy(env.get("CLAUDE_CODE_SANDBOXED")):
        return True
    try:
        with open(global_config_path(env)) as f:
            projects = json.load(f).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return None

    def accepted(d):
        entry = projects.get(d)
        return isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True

    chain, cur = [], cwd
    while True:
        chain.append(cur)
        git = os.path.join(cur, ".git")
        parent = os.path.dirname(cur)
        if (os.path.lexists(git) and not os.path.islink(git)) or parent == cur:
            break
        cur = parent
    return any(accepted(d) for d in chain)


# ============================================================================
# Which transcript is ours, and what was appended to it
# ============================================================================

def plan_session(args):
    """Decide how to find our transcript: (extra claude args, session id or None).

    A known session id pins the transcript to <project dir>/<id>.jsonl, so
    another Claude Code session in the same directory is never mistaken for
    ours. Fresh sessions get a generated --session-id; resumes without an
    explicit id (--continue, the --resume picker, --fork-session, ...) return
    None and fall back to the snapshot diff.
    """
    sid = resume_id = None
    resume = fork = False
    for i, arg in enumerate(args):
        if arg == "--":
            break
        name, eq, val = arg.partition("=")
        nxt = val if eq else (args[i + 1] if i + 1 < len(args) else None)
        if name == "--session-id":
            sid = nxt
        elif name in ("-r", "--resume"):
            resume = True
            if nxt and UUID_RE.fullmatch(nxt):
                resume_id = nxt
        elif name in RESUME_FLAGS:
            resume = True
        elif name == "--fork-session":
            fork = True
    if sid:
        return [], sid
    if resume:
        return [], (None if fork else resume_id)
    new = str(uuid.uuid4())
    return ["--session-id", new], new


def snapshot(proj):
    """Transcript inventory (path -> size) taken before start; avoids reading history."""
    snap = {}
    with contextlib.suppress(FileNotFoundError):
        for n in os.listdir(proj):
            if n.endswith(".jsonl"):
                with contextlib.suppress(OSError):
                    snap[os.path.join(proj, n)] = os.path.getsize(os.path.join(proj, n))
    return snap


def find_transcript(proj, snap, session_id=None):
    """(path, offset) of our session file, or (None, 0) while it does not exist.

    With a session id that is <proj>/<id>.jsonl. Without one: the newest file
    that is new since the snapshot or has grown (resumed; read only the tail).
    """
    if session_id:
        p = os.path.join(proj, session_id + ".jsonl")
        return (p, snap.get(p, 0)) if os.path.exists(p) else (None, 0)
    best = None
    with contextlib.suppress(FileNotFoundError):
        for n in os.listdir(proj):
            if not n.endswith(".jsonl"):
                continue
            p = os.path.join(proj, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size <= snap.get(p, -1):
                continue
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, p, snap.get(p, 0))
    return (best[1], best[2]) if best else (None, 0)


@dataclass
class Events:
    uses: list               # (tool_use_id, name, input preview)
    results: list            # tool_use_ids
    mode: Optional[str] = None
    turn_ended: bool = False


def parse_rows(data):
    """Pull tool_use / tool_result / permission mode / turn end out of JSONL bytes."""
    ev = Events([], [])
    for line in data.split(b"\n"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        ev.mode = row.get("permissionMode") or ev.mode
        message = row.get("message") or {}
        if message.get("stop_reason") in ("end_turn", "stop_sequence"):
            ev.turn_ended = True
        content = message.get("content")
        for c in content if isinstance(content, list) else ():
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                preview = json.dumps(c.get("input") or {}, ensure_ascii=False)[:110]
                ev.uses.append((c.get("id"), c.get("name"), preview))
            elif c.get("type") == "tool_result":
                ev.results.append(c.get("tool_use_id"))
    return ev


class TranscriptTail:
    """Reads only what was appended since the last call (one stat when idle)."""

    def __init__(self, proj, snap, session_id):
        self.proj, self.snap, self.session_id = proj, snap, session_id
        self.path, self.offset, self.last_size = None, 0, -1

    def read(self):
        """Events appended since the last call, or None."""
        if self.path is None:
            self.path, self.offset = find_transcript(self.proj, self.snap, self.session_id)
            if self.path is None:
                return None
            log("transcript: " + self.path)
        try:
            size = os.path.getsize(self.path)
            if size == self.last_size:
                return None
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return None
        self.last_size = size
        end = data.rfind(b"\n") + 1           # leave a half-written last line for later
        self.offset += end
        return parse_rows(data[:end])


# ============================================================================
# Approval policy: when to press Enter (or Up + Enter), and when not to
# ============================================================================

def user_typed(data, draft):
    """Track whether the user has an unsent draft in the input box."""
    if data.startswith(b"\x1b[200~"):       # bracketed paste: text lands in the input box
        return True
    if data.startswith(b"\x1b"):            # arrows / function keys / focus events
        return draft
    if any(c in data for c in (b"\r", b"\n", b"\x03", b"\x04", b"\x15")):
        return False                        # sent, interrupted or cleared
    if any(b >= 0x20 and b != 0x7f for b in data):
        return True
    return draft


@dataclass
class Tool:
    name: str
    detected: float
    tries: int = 0
    first_enter: Optional[float] = None
    last_try: float = 0.0
    held: Optional[str] = None


class Driver:
    """Transcript watcher + approval policy.

    - The first Enter for a tool_use is sent at once (harmless in an empty
      input box if the tool turns out to be auto-allowed).
    - Retries, and the Up key that would recall prompt history in the input
      box, are sent only while the TUI is quiet (see config.QUIET_*).
    - Nothing is sent while the user has a draft typed, and AskUserQuestion is
      left to the user when one is present.
    """

    def __init__(self, write, proj, snap, session_id, interactive, exit_on_idle):
        self.write = write
        self.tail = TranscriptTail(proj, snap, session_id)
        self.interactive, self.exit_on_idle = interactive, exit_on_idle
        self.tools = {}
        self.pending = set()
        self.done = set()
        self.mode = None
        self.turn_done_at = self.exit_sent_at = None
        self.out_times = deque()
        self.draft = False

    # -- inputs from the relay loop
    def on_output(self, now):
        self.out_times.append(now)

    def on_input(self, data):
        self.draft = user_typed(data, self.draft)

    def tick(self, now):
        """One poll: read the transcript, press keys. True = give up on claude."""
        ev = self.tail.read()
        if ev:
            self.apply(ev, now)
        self.approve(now)
        return self.should_stop(now)

    # -- steps
    def apply(self, ev, now):
        if ev.mode and ev.mode != self.mode:
            self.mode = ev.mode
            log(f"permissionMode = {ev.mode}")
        for tid, name, preview in ev.uses:
            if tid not in self.tools:
                self.tools[tid] = Tool(name, now)
                self.pending.add(tid)
                self.turn_done_at = self.exit_sent_at = None
                log(f"tool_use {name} ({str(tid)[-6:]}) {preview}")
        for tid in ev.results:
            t = self.tools.get(tid)
            if t and tid in self.pending:
                self.pending.discard(tid)
                self.done.add(tid)
                base = t.first_enter or t.detected
                log(f"tool_result {t.name} ({str(tid)[-6:]}) — {(now - base) * 1000:.0f}ms after enter, "
                    f"{(now - t.detected) * 1000:.0f}ms after detection")
        # After results: a tool_result and the end_turn often arrive in one read.
        if ev.turn_ended and not self.pending and self.turn_done_at is None:
            self.turn_done_at = now
            log("turn complete" + (" (quitting)" if self.exit_on_idle else ""))

    def approve(self, now):
        while self.out_times and self.out_times[0] < now - QUIET_WINDOW:
            self.out_times.popleft()
        quiet = len(self.out_times) <= QUIET_MAX_WRITES
        for tid in sorted(self.pending, key=lambda k: self.tools[k].detected):
            t = self.tools[tid]
            if t.tries >= MAX_TRIES:
                continue
            hold = ("left to the user" if self.interactive and t.name in INTERACTIVE_TOOLS
                    else "user is typing" if self.draft else None)
            if hold:
                if t.held != hold:
                    t.held = hold
                    log(f"hold {t.name} ({str(tid)[-6:]}): {hold}")
                continue
            if t.first_enter is not None and (now - t.last_try < RETRY or not quiet):
                continue
            t.tries += 1
            t.last_try = now
            if t.tries >= UP_AFTER:
                # default_to_no asks open on the decline row with no digit
                # shortcut, so move to the approve row first.
                self.write(KEY_UP)
            self.write(KEY_ENTER)                  # enter = confirm:yes
            if t.first_enter is None:
                t.first_enter = now
                log(f"enter#1 -> {t.name}")
            elif DEBUG:
                log(f"enter#{t.tries} -> {t.name} (retry)")

    def should_stop(self, now):
        """Headless / opt-in: send /exit once the turn is over; True if it was ignored."""
        if not self.exit_on_idle or self.turn_done_at is None or self.pending:
            return False
        if self.exit_sent_at is None:
            if now >= self.turn_done_at + IDLE_GRACE:
                self.write(EXIT_CMD)
                self.exit_sent_at = now
                log("sent /exit")
            return False
        if now >= self.exit_sent_at + EXIT_WAIT:
            log("claude did not exit on /exit, stopping it")
            return True
        return False


# ============================================================================
# pty / tty / child-process plumbing
# ============================================================================

def get_winsize(fd, default):
    with contextlib.suppress(OSError):
        rows, cols = struct.unpack("hhhh", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
        if rows > 0 and cols > 0:
            return rows, cols
    return default


def set_winsize(fd, rows, cols):
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("hhhh", rows, cols, 0, 0))


@contextlib.contextmanager
def raw_mode(fd):
    """Put our own tty in raw mode and always put it back."""
    saved = None
    if os.isatty(fd):
        with contextlib.suppress(OSError, termios.error):
            saved = termios.tcgetattr(fd)
            tty.setraw(fd, termios.TCSANOW)
    try:
        yield
    finally:
        if saved is not None:
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(fd, termios.TCSAFLUSH, saved)


def write_all(fd, data):
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except BlockingIOError:
            select.select([], [fd], [], 0.05)
            continue
        if n <= 0:
            return
        view = view[n:]


def stop_child(pid):
    """SIGTERM the child's process group, SIGKILL it if it lingers, then reap."""
    def alive():
        try:
            return os.waitpid(pid, os.WNOHANG)[0] == 0
        except ChildProcessError:
            return False

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not alive():
            break
        with contextlib.suppress(OSError):
            os.killpg(pid, sig)
        deadline = time.perf_counter() + EXIT_WAIT
        while sig == signal.SIGTERM and time.perf_counter() < deadline and alive():
            time.sleep(0.05)
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, 0)


# ============================================================================
# Entry point: pass-through, startup checks, relay loop
# ============================================================================

def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if NO_WRAP or (args and args[0] in PASS_THROUGH):
        os.execvp(CLAUDE, [CLAUDE] + args)     # informational / non-interactive: plain claude
    else:
        sys.exit(run(args))


def run(args):
    env, cwd = os.environ, os.getcwd()
    interactive = os.isatty(0)
    exit_on_idle = (truthy(env["CC_EXIT_ON_IDLE"]) if "CC_EXIT_ON_IDLE" in env
                    else not interactive)      # headless: quit once the turn is over
    if os.isatty(1) and LOG_TARGET:
        print(f"claude-auto: log -> {LOG_TARGET}  (pass through: CC_NO_WRAP=1 claude-auto ...)")

    proj = project_dir(cwd, env)
    extra, session_id = plan_session(args)
    cmd = [CLAUDE] + (["--permission-mode", MODE] if MODE else []) + extra + args
    log("start: " + " ".join(cmd[:6]) + " ...")
    if session_id:
        log(f"session: {session_id}")

    child_env = dict(env)
    if "CLAUDE_CODE_CHILD_SESSION" in child_env:
        # Started from inside Claude Code: the nested claude would not save its
        # transcript, leaving nothing to watch. Force persistence back on.
        child_env.setdefault("CLAUDE_CODE_FORCE_SESSION_PERSISTENCE", "1")

    snap = snapshot(proj)                      # must be taken before claude starts

    if not interactive and not TRUST_ANYWAY and workspace_trusted(cwd, env) is False:
        # The trust dialog has no keyboard to answer it here, so claude would
        # sit there forever. Fail fast with the fix instead.
        sys.stderr.write(
            f"claude-auto: {cwd} has no persisted workspace trust, so Claude Code\n"
            f"would stop at the trust dialog forever. Run `claude` once in that\n"
            f"directory and accept it, or set projects[\"{cwd}\"].hasTrustDialogAccepted\n"
            f"= true in {global_config_path(env)}. (Set CC_TRUST_ANYWAY=1 to run anyway.)\n")
        log("aborted: workspace is not trusted")
        return 2

    pid, master = pty.fork()
    if pid == 0:                               # child: controlling tty is the pty
        try:
            os.execvpe(cmd[0], cmd, child_env)
        except Exception as exc:               # noqa: BLE001 - child must not unwind
            os.write(2, f"claude-auto: exec failed: {exc}\n".encode())
        os._exit(127)

    os.set_blocking(master, False)
    size = get_winsize(0, (ROWS, COLS)) if interactive else (ROWS, COLS)
    set_winsize(master, *size)
    log(f"pty: {size[1]}x{size[0]} mode={'tty' if interactive else 'headless'}")

    def to_child(data):
        with contextlib.suppress(OSError):
            write_all(master, data)

    driver = Driver(to_child, proj, snap, session_id, interactive, exit_on_idle)
    try:
        relay(master, driver, interactive, size)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGWINCH):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, signal.SIG_IGN)     # cleanup must not be interrupted
        stop_child(pid)
        with contextlib.suppress(OSError):
            os.close(master)
        log(f"done: tool_use={len(driver.tools)} approved={len(driver.done)}")
    return 0


def relay(master, driver, interactive, size):
    """Shuttle bytes between our terminal and the pty until claude goes away."""
    flags = {"stop": False, "resize": False}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: flags.update(stop=True))
    signal.signal(signal.SIGWINCH, lambda *_: flags.update(resize=True))

    drain_until = None
    with raw_mode(0):
        while not flags["stop"]:
            try:
                readable = select.select([master] + ([0] if interactive else []), [], [], POLL)[0]
            except InterruptedError:
                readable = []
            if flags["resize"]:
                flags["resize"] = False
                if interactive:
                    set_winsize(master, *get_winsize(0, size))
            if 0 in readable:
                try:
                    data = os.read(0, 65536)
                except OSError:
                    data = b""
                if data:
                    driver.on_input(data)
                    with contextlib.suppress(OSError):
                        write_all(master, data)
            if master in readable:
                try:
                    out = os.read(master, 65536)
                except BlockingIOError:
                    out = None                 # spurious wakeup, nothing to read yet
                except OSError:
                    out = b""
                if out == b"":
                    return                     # EOF / EIO -> child closed the pty
                if out:
                    driver.on_output(time.perf_counter())
                    with contextlib.suppress(OSError):
                        write_all(1, out)
            if driver.tick(time.perf_counter()) and drain_until is None:
                # keep draining briefly so the tail of the TUI output is flushed
                drain_until = time.perf_counter() + DRAIN_WAIT
            if drain_until is not None and time.perf_counter() >= drain_until:
                return


if __name__ == "__main__":
    main()
