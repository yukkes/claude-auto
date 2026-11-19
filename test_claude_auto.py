"""Tests for claude_auto.py: python3 -m unittest -v"""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import claude_auto as ca

ca.LOG_TARGET = os.devnull                 # keep test output clean

# ---- transcript rows shared by the tests
SID = "123e4567-e89b-12d3-a456-426614174000"


def jsonl(*rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


def tool_use(tid, name="Bash"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": {"command": "ls"}}]}}


def tool_result(tid):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid}]}}


END_TURN = {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}}


# ============================================================================
# paths
# ============================================================================

class ProjectSlug(unittest.TestCase):
    """Claude Code: path.replace(/[^a-zA-Z0-9]/g, "-"), hashed past 200 chars."""

    def test_plain_path(self):
        self.assertEqual(ca.project_slug("/home/u/work/claude-auto"), "-home-u-work-claude-auto")

    def test_every_non_alnum_becomes_dash(self):
        self.assertEqual(ca.project_slug("/home/u/my_app.v2 sp"), "-home-u-my-app-v2-sp")

    def test_bmp_char_is_one_dash(self):
        self.assertEqual(ca.project_slug("/t/日本語_dir"), "-t-----dir")

    def test_non_bmp_char_is_two_dashes(self):        # UTF-16 surrogate pair
        self.assertEqual(ca.project_slug("/t/emoji😀x"), "-t-emoji--x")

    def test_long_path_is_cut_and_hashed(self):
        path = "/" + "a" * 250
        self.assertEqual(ca.project_slug(path), "-" + "a" * 199 + "-" + ca.js_hash36(path))

    def test_exactly_200_is_not_hashed(self):
        self.assertEqual(ca.project_slug("/" + "a" * 199), "-" + "a" * 199)

    def test_hash_matches_java_hashcode(self):
        self.assertEqual(ca.js_hash36("hello"), "1n1e4y")              # 99162322
        self.assertEqual(ca.js_hash36(""), "0")
        # hashCode == Integer.MIN_VALUE; JS Math.abs does not overflow
        self.assertEqual(ca.js_hash36("polygenelubricants"), "zik0zk")  # 2147483648


class ProjectDir(unittest.TestCase):
    def test_default(self):
        env = {}
        self.assertEqual(ca.project_dir("/a/b", env),
                         os.path.expanduser("~/.claude/projects/-a-b"))

    def test_config_dir(self):
        self.assertEqual(ca.project_dir("/a/b", {"CLAUDE_CONFIG_DIR": "/cfg"}), "/cfg/projects/-a-b")

    def test_override_needs_config_dir(self):
        env = {"CLAUDE_CODE_PROJECT_DIR_NAME": "mine"}
        self.assertTrue(ca.project_dir("/a/b", env).endswith("/-a-b"))
        env["CLAUDE_CONFIG_DIR"] = "/cfg"
        self.assertEqual(ca.project_dir("/a/b", env), "/cfg/projects/mine")

    def test_override_is_validated(self):
        for bad in ("con", "NUL", "a/b", "x" * 65, ""):
            env = {"CLAUDE_CONFIG_DIR": "/cfg", "CLAUDE_CODE_PROJECT_DIR_NAME": bad}
            self.assertEqual(ca.project_dir("/a/b", env), "/cfg/projects/-a-b", bad)


class GlobalConfigPath(unittest.TestCase):
    def test_paths(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ca.global_config_path({"CLAUDE_CONFIG_DIR": d}), os.path.join(d, ".claude.json"))
            env = {"CLAUDE_CONFIG_DIR": d, "CLAUDE_CODE_CUSTOM_OAUTH_URL": "https://x"}
            self.assertEqual(ca.global_config_path(env), os.path.join(d, ".claude-custom-oauth.json"))
            legacy = os.path.join(d, ".config.json")
            open(legacy, "w").close()                       # empty legacy file is ignored
            self.assertEqual(ca.global_config_path({"CLAUDE_CONFIG_DIR": d}), os.path.join(d, ".claude.json"))
            with open(legacy, "w") as f:
                f.write("{}")
            self.assertEqual(ca.global_config_path({"CLAUDE_CONFIG_DIR": d}), legacy)

    def test_default_is_home(self):
        self.assertEqual(ca.global_config_path({}), os.path.expanduser("~/.claude.json"))


class WorkspaceTrusted(unittest.TestCase):
    """Behaviour checked against the real trust dialog of Claude Code 2.1.327."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.cfg = os.path.join(self.root, "cfg")
        os.makedirs(self.cfg)
        self.env = {"CLAUDE_CONFIG_DIR": self.cfg}
        self.trusted = os.path.join(self.root, "work")
        for d in ("work/a/b", "work/repo/.git", "work/repo/sub", "other"):
            os.makedirs(os.path.join(self.root, d))
        self.write_trust({self.trusted: True})

    def tearDown(self):
        self.tmp.cleanup()

    def write_trust(self, mapping):
        projects = {k: {"hasTrustDialogAccepted": v} for k, v in mapping.items()}
        with open(os.path.join(self.cfg, ".claude.json"), "w") as f:
            json.dump({"projects": projects}, f)

    def p(self, rel):
        return os.path.join(self.root, rel)

    def test_trusted_dir_and_subdirs(self):
        self.assertIs(ca.workspace_trusted(self.trusted, self.env), True)
        self.assertIs(ca.workspace_trusted(self.p("work/a/b"), self.env), True)

    def test_git_repo_stops_the_walk(self):
        self.assertIs(ca.workspace_trusted(self.p("work/repo"), self.env), False)
        self.assertIs(ca.workspace_trusted(self.p("work/repo/sub"), self.env), False)

    def test_trusted_repo_root_covers_subdirs(self):
        self.write_trust({self.p("work/repo"): True})
        self.assertIs(ca.workspace_trusted(self.p("work/repo/sub"), self.env), True)

    def test_untrusted_and_explicit_false(self):
        self.assertIs(ca.workspace_trusted(self.p("other"), self.env), False)
        self.write_trust({self.trusted: False})
        self.assertIs(ca.workspace_trusted(self.trusted, self.env), False)

    def test_sandboxed_skips_the_check(self):
        env = dict(self.env, CLAUDE_CODE_SANDBOXED="1")
        self.assertIs(ca.workspace_trusted(self.p("other"), env), True)

    def test_unreadable_config(self):
        os.remove(os.path.join(self.cfg, ".claude.json"))
        self.assertIsNone(ca.workspace_trusted(self.trusted, self.env))
        with open(os.path.join(self.cfg, ".claude.json"), "w") as f:
            f.write("not json")
        self.assertIsNone(ca.workspace_trusted(self.trusted, self.env))


# ============================================================================
# session
# ============================================================================

class PlanSession(unittest.TestCase):
    def assertGenerated(self, args):
        extra, sid = ca.plan_session(args)
        self.assertEqual(extra, ["--session-id", sid])
        self.assertRegex(sid, ca.UUID_RE)

    def test_fresh_sessions_get_an_id(self):
        for args in ([], ["hello"], ["--model", "haiku", "fix it"], ["--", "-c"]):
            self.assertGenerated(args)

    def test_known_ids(self):
        for args in (["--session-id", SID], [f"--session-id={SID}"], ["-r", SID],
                     ["--resume", SID, "go"], [f"--resume={SID}"],
                     ["--continue", "--fork-session", "--session-id", SID]):
            self.assertEqual(ca.plan_session(args), ([], SID), args)

    def test_unknown_resumes_fall_back(self):
        for args in (["-r"], ["-r", "search term"], ["-c"], ["--continue", "go"],
                     ["-r", SID, "--fork-session"], ["--from-pr", "12"], ["--teleport"]):
            self.assertEqual(ca.plan_session(args), ([], None), args)


class FindTranscript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, text):
        path = os.path.join(self.proj, name)
        with open(path, "a") as f:
            f.write(text)
        return path

    def test_pinned_session(self):
        other = self.put("other.jsonl", "x\n")
        snap = ca.snapshot(self.proj)
        self.put("other.jsonl", "grown\n")
        self.assertEqual(ca.find_transcript(self.proj, snap, SID), (None, 0))
        mine = self.put(SID + ".jsonl", "y\n")
        self.assertEqual(ca.find_transcript(self.proj, snap, SID), (mine, 0))
        self.assertNotEqual(mine, other)

    def test_pinned_resume_reads_only_the_tail(self):
        mine = self.put(SID + ".jsonl", "old\n")
        snap = ca.snapshot(self.proj)
        self.assertEqual(ca.find_transcript(self.proj, snap, SID), (mine, 4))

    def test_fallback_new_or_grown(self):
        old = self.put("old.jsonl", "abc\n")
        snap = ca.snapshot(self.proj)
        self.assertEqual(ca.find_transcript(self.proj, snap), (None, 0))
        self.put("old.jsonl", "more\n")
        self.assertEqual(ca.find_transcript(self.proj, snap), (old, 4))

    def test_missing_dir(self):
        missing = os.path.join(self.proj, "nope")
        self.assertEqual(ca.snapshot(missing), {})
        self.assertEqual(ca.find_transcript(missing, {}), (None, 0))


class ParseRows(unittest.TestCase):
    def test_events(self):
        data = jsonl({"type": "permission-mode", "permissionMode": "default"},
                     tool_use("t1"), tool_result("t1"), END_TURN).encode()
        ev = ca.parse_rows(data + b"garbage\n[1]\n")
        self.assertEqual([u[:2] for u in ev.uses], [("t1", "Bash")])
        self.assertEqual(ev.results, ["t1"])
        self.assertEqual(ev.mode, "default")
        self.assertTrue(ev.turn_ended)

    def test_odd_content_is_ignored(self):
        ev = ca.parse_rows(jsonl({"message": {"content": ["text", 3, None]}}).encode())
        self.assertEqual((ev.uses, ev.results), ([], []))


# ============================================================================
# driver
# ============================================================================

class UserTyped(unittest.TestCase):
    def test_cases(self):
        cases = [((b"abc", False), True), ((b"\r", True), False), ((b"x\r", False), False),
                 ((b"\x1b[A", True), True), ((b"\x1b[A", False), False), ((b"\x03", True), False),
                 ((b"\x15", True), False), ((b"\x1b[200~hi\x1b[201~", False), True),
                 ((b"\x7f", True), True)]
        for (data, draft), want in cases:
            self.assertEqual(ca.user_typed(data, draft), want, (data, draft))


class DriverPolicy(unittest.TestCase):
    """Approval policy, driven with a fake clock and a key recorder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = self.tmp.name
        self.keys = []
        self.path = os.path.join(self.proj, SID + ".jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def driver(self, interactive=False, exit_on_idle=False):
        return ca.Driver(self.keys.append, self.proj, {}, SID, interactive, exit_on_idle)

    def append(self, *rows):
        with open(self.path, "a") as f:
            f.write(jsonl(*rows))

    def test_first_enter_is_immediate_even_when_busy(self):
        d = self.driver()
        self.append(tool_use("t1"))
        for i in range(10):
            d.on_output(i * 0.01)                         # TUI is busy
        d.tick(0.1)
        self.assertEqual(self.keys, [ca.KEY_ENTER])

    def test_no_retry_while_tui_is_busy(self):
        d = self.driver()
        for i in range(10):                               # spinner was already running
            d.on_output(-1.0 + i * 0.1)
        self.append(tool_use("t1"))
        d.tick(0.0)
        now = 0.0
        while now < 5.0:                                  # spinner: 10 writes/s
            now += 0.1
            d.on_output(now)
            d.tick(now)
        self.assertEqual(self.keys, [ca.KEY_ENTER])

    def test_retry_when_quiet_then_up(self):
        d = self.driver()
        self.append(tool_use("t1"))
        now = 0.0
        while now < 10.0:
            d.tick(now)
            now += 0.05
        self.assertEqual(self.keys.count(ca.KEY_ENTER), ca.MAX_TRIES)
        self.assertEqual(self.keys.count(ca.KEY_UP), ca.MAX_TRIES - ca.UP_AFTER + 1)
        first_up = self.keys.index(ca.KEY_UP)
        self.assertEqual(self.keys[:first_up].count(ca.KEY_ENTER), ca.UP_AFTER - 1)

    def test_result_stops_retries(self):
        d = self.driver()
        self.append(tool_use("t1"))
        d.tick(0.0)
        self.append(tool_result("t1"))
        d.tick(1.0)
        d.tick(2.0)
        self.assertEqual(self.keys, [ca.KEY_ENTER])
        self.assertEqual(d.done, {"t1"})

    def test_hold_while_user_has_a_draft(self):
        d = self.driver(interactive=True)
        d.on_input(b"half a sentence")
        self.append(tool_use("t1"))
        d.tick(0.0)
        d.tick(1.0)
        self.assertEqual(self.keys, [])
        d.on_input(b"\x15")                               # Ctrl-U clears the draft
        d.tick(2.0)
        self.assertEqual(self.keys, [ca.KEY_ENTER])

    def test_ask_user_question_is_left_to_a_present_user(self):
        d = self.driver(interactive=True)
        self.append(tool_use("q1", "AskUserQuestion"))
        d.tick(0.0)
        self.assertEqual(self.keys, [])
        headless = self.driver(interactive=False)
        headless.tick(0.0)
        self.assertEqual(self.keys, [ca.KEY_ENTER])

    def test_exit_on_idle(self):
        d = self.driver(exit_on_idle=True)
        self.append(tool_use("t1"))
        d.tick(0.0)
        self.append(tool_result("t1"), END_TURN)
        d.tick(1.0)
        self.assertFalse(d.tick(1.0 + ca.IDLE_GRACE / 2))
        self.assertNotIn(ca.EXIT_CMD, self.keys)
        self.assertFalse(d.tick(1.0 + ca.IDLE_GRACE))
        self.assertEqual(self.keys[-1], ca.EXIT_CMD)
        self.assertTrue(d.tick(1.0 + ca.IDLE_GRACE + ca.EXIT_WAIT))

    def test_new_tool_use_cancels_pending_exit(self):
        d = self.driver(exit_on_idle=True)
        self.append(END_TURN)
        d.tick(0.0)
        self.append(tool_use("t2"))
        d.tick(0.1)
        d.tick(10.0)
        self.assertNotIn(ca.EXIT_CMD, self.keys)

    def test_half_written_line_is_read_later(self):
        d = self.driver()
        line = json.dumps(tool_use("t1"))
        with open(self.path, "a") as f:
            f.write(line[:20])
        d.tick(0.0)
        self.assertEqual(self.keys, [])
        with open(self.path, "a") as f:
            f.write(line[20:] + "\n")
        d.tick(0.1)
        self.assertEqual(self.keys, [ca.KEY_ENTER])


# ============================================================================
# cli
# ============================================================================

class PassThrough(unittest.TestCase):
    def test_informational_commands_run_plain_claude(self):
        for first in ("--version", "-p", "doctor", "mcp"):
            with mock.patch.object(ca.os, "execvp") as execvp, \
                    mock.patch.object(ca, "run") as run:
                ca.main([first, "x"])
            execvp.assert_called_once_with(ca.CLAUDE, [ca.CLAUDE, first, "x"])
            run.assert_not_called()

    def test_no_wrap(self):
        with mock.patch.object(ca, "NO_WRAP", True), \
                mock.patch.object(ca.os, "execvp") as execvp, mock.patch.object(ca, "run"):
            ca.main(["fix it"])
        execvp.assert_called_once()

    def test_prompt_is_wrapped(self):
        with mock.patch.object(ca.os, "execvp") as execvp, \
                mock.patch.object(ca, "run", return_value=0) as run:
            with self.assertRaises(SystemExit):
                ca.main(["fix it", "-p"])        # -p only counts as the first argument
        execvp.assert_not_called()
        run.assert_called_once_with(["fix it", "-p"])


class HeadlessTrustCheck(unittest.TestCase):
    def test_untrusted_dir_exits_2_before_starting_claude(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "cfg")
            work = os.path.join(d, "work")
            os.makedirs(cfg)
            os.makedirs(work)
            with open(os.path.join(cfg, ".claude.json"), "w") as f:
                json.dump({"projects": {}}, f)
            env = {"CLAUDE_CONFIG_DIR": cfg}
            err = io.StringIO()
            with mock.patch.dict(ca.os.environ, env, clear=True), \
                    mock.patch.object(ca.os, "getcwd", return_value=work), \
                    mock.patch.object(ca.os, "isatty", return_value=False), \
                    mock.patch.object(ca.pty, "fork") as fork, \
                    mock.patch.object(ca.sys, "stderr", err):
                self.assertEqual(ca.run(["hello"]), 2)
            fork.assert_not_called()
            self.assertIn("no persisted workspace trust", err.getvalue())
            self.assertIn(os.path.join(cfg, ".claude.json"), err.getvalue())


if __name__ == "__main__":
    unittest.main()
