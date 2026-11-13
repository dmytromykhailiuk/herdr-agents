import os
import stat
import subprocess
import sys
import unittest

from _base import CLI, IsolatedTestCase


class ShimTests(IsolatedTestCase):
    def fake_python(self, name="python3.14", body=None):
        d = self.root / "bin"
        d.mkdir(exist_ok=True)
        p = d / name
        marker = self.root / "exec.log"
        p.write_text(body or f'#!/bin/sh\necho "$@" > "{marker}"\ncat >> "{marker}"\nexit 7\n')
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        return d, marker

    def run_shim(self, args, extra_env, stdin=""):
        env = self.make_env()
        env.update(extra_env)
        return subprocess.run([sys.executable, str(CLI)] + args, input=stdin, capture_output=True,
                              text=True, env=env)

    def test_reexec_under_found_interpreter(self):
        d, marker = self.fake_python()
        proc = self.run_shim(["hook", "x"], {"HERDR_AGENTS_TEST_FORCE_OLD": "1", "PATH": f"{d}:/usr/bin:/bin"},
                             stdin="payload")
        self.assertEqual(proc.returncode, 7)
        text = marker.read_text()
        self.assertIn(str(CLI), text)
        self.assertIn("hook x", text)
        self.assertIn("payload", text)

    def test_env_override_first(self):
        d, _ = self.fake_python()
        other, marker = self.fake_python("custom-py")
        proc = self.run_shim(["status"], {"HERDR_AGENTS_TEST_FORCE_OLD": "1",
                                          "HERDR_AGENTS_PYTHON": str(other / "custom-py"),
                                          "PATH": f"{d}:/usr/bin:/bin"})
        self.assertEqual(proc.returncode, 7)
        self.assertTrue(marker.exists())

    def test_loop_guard(self):
        d, marker = self.fake_python()
        proc = self.run_shim(["status"], {"HERDR_AGENTS_TEST_FORCE_OLD": "1", "HERDR_AGENTS_REEXEC": "1",
                                          "PATH": f"{d}:/usr/bin:/bin"})
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(marker.exists())
        self.assertIn("Python 3.10", proc.stderr)

    def test_none_found_hook_exits_zero(self):
        empty = self.root / "empty"
        empty.mkdir()
        proc = self.run_shim(["hook", "x"], {"HERDR_AGENTS_TEST_FORCE_OLD": "1", "PATH": str(empty),
                                             "HERDR_AGENTS_REEXEC": "1"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)

    def test_none_found_other_exits_one(self):
        proc = self.run_shim(["status"], {"HERDR_AGENTS_TEST_FORCE_OLD": "1", "HERDR_AGENTS_REEXEC": "1"})
        self.assertEqual(proc.returncode, 1)

    def test_loop_guard_not_leaked(self):
        code = ("import os, runpy, sys\n"
                f"sys.argv = [{str(CLI)!r}, 'path']\n"
                "try:\n    runpy.run_path(sys.argv[0], run_name='__main__')\n"
                "except SystemExit:\n    pass\n"
                "sys.stderr.write('guard=' + os.environ.get('HERDR_AGENTS_REEXEC', '-'))\n")
        env = self.make_env()
        env["HERDR_AGENTS_REEXEC"] = "1"
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        self.assertIn("guard=-", proc.stderr)

    def test_shim_parses_on_old_syntax(self):
        import ast
        ast.parse(CLI.read_text(), feature_version=(3, 6))


if __name__ == "__main__":
    unittest.main()
