import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from _base import LIB, REPO, IsolatedTestCase

sys.path.insert(0, str(LIB))

from herdragent import herdrconfig  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"

RUNNER = (
    "import os, sys\n"
    f"sys.path.insert(0, {str(LIB)!r})\n"
    "from herdragent import core, setup\n"
    "core.CLI_PATH = os.environ['TEST_CLI']\n"
    "sys.exit(setup.main(sys.argv[1:]))\n"
)


class SetupTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.install = self.root / "install"
        self.cli = self.make_install(self.install)
        self.settings = self.claude_dir / "settings.json"
        self.config = Path(self.env["HERDR_CONFIG_PATH"])
        self.bin_dir = self.root / "bin"
        self.env["HERDR_AGENT_BIN_DIR"] = str(self.bin_dir)
        self.env["TEST_CLI"] = str(self.cli)

    def make_install(self, root, bundle=False):
        cli = root / "skills" / "herdr-agent" / "bin" / "herdr-agent"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        cli.chmod(0o755)
        shutil.copytree(REPO / "herdr-plugin", root / "herdr-plugin", ignore=shutil.ignore_patterns("__pycache__"))
        if bundle:
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        return cli

    def setup_cmd(self, *args, env=None, cli=None):
        e = dict(env or self.env)
        if cli:
            e["TEST_CLI"] = str(cli)
        proc = subprocess.run([sys.executable, "-c", RUNNER, *args], env=e, capture_output=True, text=True,
                              cwd=str(self.root), timeout=60)
        return proc

    def settings_data(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def hook_commands(self, data):
        out = []
        for event, groups in data.get("hooks", {}).items():
            for g in groups:
                for h in g["hooks"]:
                    out.append((event, g.get("matcher"), h["command"], h.get("timeout")))
        return out


class HookPermsTest(SetupTestCase):
    def test_idempotent_bytes(self):
        p1 = self.setup_cmd("setup", "--only", "hook,perms")
        self.assertEqual(p1.returncode, 0, p1.stderr)
        first = self.settings.read_bytes()
        p2 = self.setup_cmd("setup", "--only", "hook,perms")
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertEqual(first, self.settings.read_bytes())

    def test_hooks_shape(self):
        self.setup_cmd("setup", "--only", "hook")
        cmds = self.hook_commands(self.settings_data())
        self.assertEqual(len(cmds), 4)
        self.assertEqual(cmds[0], ("SessionStart", "^(startup|resume|clear|compact)$",
                                   f"'{self.cli}' hook session-start", 10))
        self.assertEqual(cmds[1], ("SessionEnd", "^(prompt_input_exit|logout)$",
                                   f"'{self.cli}' hook session-end", 5))
        self.assertEqual(cmds[2], ("PreToolUse", "^(Agent|Task)$", f"'{self.cli}' hook pre-agent", 5))
        self.assertEqual(cmds[3], ("PreToolUse", "^Bash$", f"'{self.cli}' hook pre-bash", 5))

    def test_perms_rules(self):
        self.setup_cmd("setup", "--only", "perms")
        allow = self.settings_data()["permissions"]["allow"]
        for sub in ("run", "spawn", "wait", "send", "status", "result", "list", "screen"):
            self.assertIn(f"Bash({self.cli} {sub} *)", allow)
        self.assertNotIn(f"Bash({self.cli} keys *)", allow)

    def test_dry_run_writes_nothing(self):
        p = self.setup_cmd("setup", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(self.settings.exists())
        self.assertIn("would write", p.stdout)
        self.assertFalse((self.data_dir / "herdr-plugin").exists())
        self.assertFalse((self.state_dir / "installed.json").exists())
        self.assertFalse(self.config.exists())
        self.assertEqual(self.fake_calls(mutating=True), [])

    def test_foreign_entries_preserved_and_uninstall(self):
        foreign = {
            "model": "opus",
            "permissions": {"allow": ["Bash(ls *)"]},
            "hooks": {
                "SessionStart": [{"matcher": "x", "hooks": [{"type": "command", "command": "echo hi"}]}],
                "PreToolUse": [{"matcher": "^Bash$", "hooks": [{"type": "command", "command": "other.sh"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "stop.sh"}]}],
            },
        }
        self.settings.write_text(json.dumps(foreign), encoding="utf-8")
        self.setup_cmd("setup", "--only", "hook,perms")
        data = self.settings_data()
        self.assertEqual(len(self.hook_commands(data)), 3 + 4)
        self.assertIn("Bash(ls *)", data["permissions"]["allow"])
        self.setup_cmd("setup", "--only", "hook,perms", "--uninstall")
        self.assertEqual(self.settings_data(), foreign)

    def test_uninstall_on_clean_install_drops_keys(self):
        self.settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        self.setup_cmd("setup", "--only", "hook,perms")
        self.setup_cmd("setup", "--only", "hook,perms", "--uninstall")
        self.assertEqual(self.settings_data(), {"model": "opus"})

    def test_moved_cli_path_replaces_old_hooks(self):
        self.setup_cmd("setup", "--only", "hook,perms")
        moved = self.make_install(self.root / "moved")
        p = self.setup_cmd("setup", "--only", "hook,perms", cli=moved)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = self.settings_data()
        cmds = self.hook_commands(data)
        self.assertEqual(len(cmds), 4)
        self.assertTrue(all(str(moved) in c[2] for c in cmds))
        self.assertFalse(any(str(self.cli) in r for r in data["permissions"]["allow"]))

    def test_backup_created_once(self):
        self.settings.write_text('{"model": "opus"}', encoding="utf-8")
        self.setup_cmd("setup", "--only", "hook")
        bak = self.settings.with_name("settings.json.bak")
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8")), {"model": "opus"})
        self.setup_cmd("setup", "--only", "perms")
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8")), {"model": "opus"})

    def test_mode_preserved(self):
        self.settings.write_text("{}", encoding="utf-8")
        os.chmod(self.settings, 0o600)
        self.setup_cmd("setup", "--only", "hook")
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_invalid_json_refused(self):
        self.settings.write_text("{not json", encoding="utf-8")
        p = self.setup_cmd("setup", "--only", "hook")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")

    def test_bundle_skips_hook_but_not_perms(self):
        cli = self.make_install(self.root / "bundle", bundle=True)
        p = self.setup_cmd("setup", "--only", "hook,perms", cli=cli)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("skipped hook", p.stdout)
        data = self.settings_data()
        self.assertNotIn("hooks", data)
        self.assertTrue(data["permissions"]["allow"])
        p = self.setup_cmd("setup", "--only", "hook", "--force", cli=cli)
        self.assertEqual(len(self.hook_commands(self.settings_data())), 4)

    def test_project_scope(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=self.env)
        proc = subprocess.run([sys.executable, "-c", RUNNER, "setup", "--scope", "project"], env=self.env,
                              capture_output=True, text=True, cwd=str(repo), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        target = repo / ".claude" / "settings.json"
        self.assertEqual(len(self.hook_commands(json.loads(target.read_text(encoding="utf-8")))), 4)
        self.assertFalse(self.settings.exists())
        self.assertFalse((self.data_dir / "herdr-plugin").exists())

    def test_unknown_part(self):
        p = self.setup_cmd("setup", "--only", "bogus")
        self.assertNotEqual(p.returncode, 0)


class BinTest(SetupTestCase):
    def test_link_created_and_idempotent(self):
        p = self.setup_cmd("setup", "--only", "bin")
        self.assertEqual(p.returncode, 0, p.stderr)
        link = self.bin_dir / "herdr-agent"
        self.assertEqual(os.readlink(link), str(self.cli))
        self.assertEqual(self.setup_cmd("setup", "--only", "bin").returncode, 0)

    def test_refuses_regular_file_and_foreign_symlink(self):
        self.bin_dir.mkdir()
        link = self.bin_dir / "herdr-agent"
        link.write_text("mine", encoding="utf-8")
        p = self.setup_cmd("setup", "--only", "bin")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(link.read_text(encoding="utf-8"), "mine")
        link.unlink()
        other = self.root / "other-tool"
        other.write_text("x", encoding="utf-8")
        link.symlink_to(other)
        self.assertNotEqual(self.setup_cmd("setup", "--only", "bin").returncode, 0)
        self.assertEqual(os.readlink(link), str(other))
        self.assertEqual(self.setup_cmd("setup", "--only", "bin", "--force").returncode, 0)
        self.assertEqual(os.readlink(link), str(self.cli))

    def test_moved_cli_updates_own_link(self):
        self.setup_cmd("setup", "--only", "bin")
        moved = self.make_install(self.root / "m2")
        self.assertEqual(self.setup_cmd("setup", "--only", "bin", cli=moved).returncode, 0)
        self.assertEqual(os.readlink(self.bin_dir / "herdr-agent"), str(moved))

    def test_uninstall_removes_only_ours(self):
        self.setup_cmd("setup", "--only", "bin")
        self.setup_cmd("setup", "--only", "bin", "--uninstall")
        self.assertFalse(os.path.lexists(self.bin_dir / "herdr-agent"))
        other = self.root / "other-tool"
        other.write_text("x", encoding="utf-8")
        (self.bin_dir / "herdr-agent").symlink_to(other)
        self.setup_cmd("setup", "--only", "bin", "--uninstall")
        self.assertTrue(os.path.islink(self.bin_dir / "herdr-agent"))

    def test_perms_include_bin_link(self):
        self.setup_cmd("setup", "--only", "bin,perms")
        allow = self.settings_data()["permissions"]["allow"]
        self.assertIn(f"Bash({self.bin_dir / 'herdr-agent'} run *)", allow)


class HerdrPartTest(SetupTestCase):
    def test_full_install(self):
        p = self.setup_cmd("setup", "--only", "herdr")
        self.assertEqual(p.returncode, 0, p.stderr)
        copy = self.data_dir / "herdr-plugin"
        manifest = (copy / "herdr-plugin.toml").read_text(encoding="utf-8")
        self.assertIn(f'command = [{json.dumps(sys.executable)}, "startup.py"]', manifest)
        self.assertTrue((copy / "startup.py").is_file())
        pointer = json.loads((self.state_dir / "cli.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["cli"], str(self.cli))
        self.assertIn("python", pointer)
        self.assertIn("resume_agents_on_restore = true", self.config.read_text(encoding="utf-8"))
        calls = self.fake_calls(mutating=True)
        self.assertIn(["plugin", "link", str(copy)], calls)
        self.assertIn(["server", "reload-config"], calls)
        installed = json.loads((self.state_dir / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["herdr_plugin_dir"], str(copy))
        self.assertIn("herdr", installed["parts"])

    def test_second_run_makes_no_mutating_calls(self):
        self.setup_cmd("setup", "--only", "herdr")
        self.fake_set(calls=[])
        p = self.setup_cmd("setup", "--only", "herdr")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.fake_calls(mutating=True), [])

    def test_relinks_when_linked_elsewhere(self):
        self.fake_set(plugins=[{"id": "dmytromykhailiuk.herdr-agents", "path": "/old/place", "enabled": True}])
        self.setup_cmd("setup", "--only", "herdr")
        calls = self.fake_calls(mutating=True)
        self.assertIn(["plugin", "unlink", "dmytromykhailiuk.herdr-agents"], calls)
        self.assertIn(["plugin", "link", str(self.data_dir / "herdr-plugin")], calls)

    def test_old_herdr_rejected(self):
        self.fake_set(version="0.9.1")
        p = self.setup_cmd("setup", "--only", "herdr")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("too old", p.stderr)
        self.assertFalse((self.data_dir / "herdr-plugin").exists())

    def test_no_server_still_succeeds(self):
        self.fake_set(server_running=False)
        p = self.setup_cmd("setup", "--only", "herdr")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_explicit_false_kept_with_warning(self):
        self.config.parent.mkdir(parents=True)
        shutil.copy(FIXTURES / "config-false.toml", self.config)
        p = self.setup_cmd("setup", "--only", "herdr")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("warning", p.stdout)
        self.assertEqual(self.config.read_bytes(), (FIXTURES / "config-false.toml").read_bytes())
        self.assertNotIn(["server", "reload-config"], self.fake_calls(mutating=True))

    def test_uninstall(self):
        self.setup_cmd("setup", "--only", "herdr")
        p = self.setup_cmd("setup", "--only", "herdr", "--uninstall")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(["plugin", "unlink", "dmytromykhailiuk.herdr-agents"], self.fake_calls(mutating=True))
        self.assertFalse((self.data_dir / "herdr-plugin").exists())
        self.assertTrue(self.config.exists())
        self.assertFalse((self.state_dir / "installed.json").exists())

    def test_missing_plugin_source(self):
        shutil.rmtree(self.install / "herdr-plugin")
        p = self.setup_cmd("setup", "--only", "herdr")
        self.assertNotEqual(p.returncode, 0)


class ForeignOrchestratorHookTest(SetupTestCase):
    FOREIGN = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
        {"type": "command", "command": "python3 ~/.claude/hooks/inject_orchestrator.py"}]}]}}

    def write_foreign(self):
        self.settings.write_text(json.dumps(self.FOREIGN), encoding="utf-8")

    def test_setup_warns_and_keeps_foreign_hook(self):
        self.write_foreign()
        p = self.setup_cmd("setup")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("injects orchestrator rules", p.stdout)
        self.assertIn("inject_orchestrator.py", p.stdout)
        commands = [c for _, _, c, _ in self.hook_commands(self.settings_data())]
        self.assertIn("python3 ~/.claude/hooks/inject_orchestrator.py", commands)

    def test_setup_silent_without_foreign_hook(self):
        p = self.setup_cmd("setup")
        self.assertNotIn("orchestrator", p.stdout)

    def test_doctor_warns(self):
        self.setup_cmd("setup")
        data = self.settings_data()
        data["hooks"]["SessionStart"].extend(self.FOREIGN["hooks"]["SessionStart"])
        self.settings.write_text(json.dumps(data), encoding="utf-8")
        p = self.setup_cmd("doctor", "--json")
        checks = {c["name"]: c for c in json.loads(p.stdout)["checks"]}
        self.assertEqual(checks["orchestrator_hook_user"]["status"], "warn")
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_doctor_no_warning_after_clean_setup(self):
        self.setup_cmd("setup")
        checks = {c["name"] for c in json.loads(self.setup_cmd("doctor", "--json").stdout)["checks"]}
        self.assertNotIn("orchestrator_hook_user", checks)


class DoctorTest(SetupTestCase):
    def doctor(self, env=None):
        p = self.setup_cmd("doctor", "--json", env=env)
        return p, json.loads(p.stdout)

    def by_name(self, report):
        return {c["name"]: c for c in report["checks"]}

    def test_fresh_fails(self):
        p, report = self.doctor()
        self.assertEqual(p.returncode, 1)
        self.assertFalse(report["ok"])
        checks = self.by_name(report)
        self.assertEqual(checks["hooks_user"]["status"], "fail")
        self.assertEqual(checks["herdr_plugin"]["status"], "fail")

    def test_after_setup_passes(self):
        self.setup_cmd("setup")
        p, report = self.doctor()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(report["ok"])
        checks = self.by_name(report)
        for name in ("python", "herdr", "hooks_user", "perms", "herdr_plugin", "cli_pointer",
                     "resume_agents_on_restore", "claude", "records"):
            self.assertEqual(checks[name]["status"], "ok", checks[name])
        self.assertEqual(checks["herdr_env"]["status"], "warn")

    def test_old_herdr_fails(self):
        self.setup_cmd("setup")
        self.fake_set(version="0.9.0")
        p, report = self.doctor()
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.by_name(report)["herdr"]["status"], "fail")

    def test_resume_false_warns(self):
        self.setup_cmd("setup")
        self.config.write_text("[session]\nresume_agents_on_restore = false\n", encoding="utf-8")
        p, report = self.doctor()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.by_name(report)["resume_agents_on_restore"]["status"], "warn")

    def test_bundle_plus_settings_hooks_warns(self):
        self.setup_cmd("setup", "--only", "hook,perms")
        cli = self.make_install(self.root / "bundle", bundle=True)
        e = dict(self.env, TEST_CLI=str(cli))
        p, report = self.doctor(env=e)
        self.assertEqual(self.by_name(report)["hooks_user"]["status"], "warn")

    def test_human_output(self):
        p = self.setup_cmd("doctor")
        self.assertIn("[FAIL]", p.stdout)
        self.assertEqual(p.returncode, 1)

    def test_broken_bin_link_fails(self):
        self.setup_cmd("setup")
        self.bin_dir.mkdir()
        (self.bin_dir / "herdr-agent").symlink_to(self.root / "nowhere")
        p, report = self.doctor()
        self.assertEqual(self.by_name(report)["bin"]["status"], "fail")


class DoctorForeignInstallTest(SetupTestCase):
    doctor = DoctorTest.doctor
    by_name = DoctorTest.by_name

    def test_bin_and_perms_of_other_install_warn(self):
        old = self.make_install(self.root / "old")
        self.setup_cmd("setup", "--only", "bin,perms", cli=old)
        p, report = self.doctor()
        checks = self.by_name(report)
        self.assertEqual(checks["bin"]["status"], "warn", checks["bin"])
        self.assertIn("another herdr-agent install found at", checks["bin"]["detail"])
        self.assertIn(os.path.realpath(old), checks["bin"]["detail"])
        self.assertEqual(checks["perms"]["status"], "warn", checks["perms"])
        self.assertIn("another herdr-agent install found at", checks["perms"]["detail"])

    def test_bin_and_perms_of_running_cli_ok(self):
        self.setup_cmd("setup", "--only", "bin,perms")
        p, report = self.doctor()
        checks = self.by_name(report)
        self.assertEqual(checks["bin"]["status"], "ok", checks["bin"])
        self.assertEqual(checks["perms"]["status"], "ok", checks["perms"])
        self.assertIn(os.path.realpath(self.cli), checks["perms"]["detail"])


class HerdrConfigTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "config.toml"

    def use(self, name):
        shutil.copy(FIXTURES / name, self.path)
        return self.path.read_text(encoding="utf-8")

    def test_missing_file_created(self):
        self.assertTrue(herdrconfig.ensure_resume_on(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[session]\nresume_agents_on_restore = true\n")
        self.assertFalse(self.path.with_name("config.toml.bak").exists())

    def test_no_session_table_appended(self):
        original = self.use("config-no-session.toml")
        self.assertTrue(herdrconfig.ensure_resume_on(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         original + "\n[session]\nresume_agents_on_restore = true\n")
        self.assertEqual(self.path.with_name("config.toml.bak").read_text(encoding="utf-8"), original)
        self.assertFalse(herdrconfig.ensure_resume_on(self.path))

    def test_commented_key_gets_real_key_after_header(self):
        self.use("config-commented-key.toml")
        self.assertTrue(herdrconfig.ensure_resume_on(self.path))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        i = lines.index("[session]")
        self.assertEqual(lines[i + 1], "resume_agents_on_restore = true")
        self.assertEqual(lines[i + 2], "# resume_agents_on_restore = true")
        self.assertIn("[keys]", lines)
        self.assertFalse(herdrconfig.ensure_resume_on(self.path))

    def test_spaced_header_and_other_tables(self):
        self.use("config-session-other-keys.toml")
        self.assertTrue(herdrconfig.ensure_resume_on(self.path))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        i = lines.index("[ session ]")
        self.assertEqual(lines[i + 1], "resume_agents_on_restore = true")
        self.assertIn("[[startup_item]]", lines)
        self.assertEqual(herdrconfig.state(self.path), "true")

    def test_explicit_false_untouched(self):
        original = self.use("config-false.toml")
        self.assertFalse(herdrconfig.ensure_resume_on(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.assertEqual(herdrconfig.state(self.path), "false")

    def test_explicit_true_noop(self):
        original = self.use("config-true.toml")
        self.assertFalse(herdrconfig.ensure_resume_on(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_key_in_other_table_does_not_count(self):
        self.path.write_text("[ui]\nresume_agents_on_restore = false\n\n[session]\nx = 1\n", encoding="utf-8")
        self.assertTrue(herdrconfig.ensure_resume_on(self.path))
        self.assertEqual(herdrconfig.state(self.path), "true")

    def test_no_trailing_newline(self):
        self.path.write_text('[ui]\ntheme = "x"', encoding="utf-8")
        herdrconfig.ensure_resume_on(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         '[ui]\ntheme = "x"\n\n[session]\nresume_agents_on_restore = true\n')

    def test_dry_run_writes_nothing(self):
        self.assertTrue(herdrconfig.ensure_resume_on(self.path, dry_run=True))
        self.assertFalse(self.path.exists())


class StartupLauncherTest(IsolatedTestCase):
    def test_execs_cli_from_pointer(self):
        fake_cli = self.root / "fake-cli.py"
        fake_cli.write_text("import sys\nprint('ARGS', sys.argv[1:])\n", encoding="utf-8")
        (self.state_dir / "cli.json").write_text(
            json.dumps({"cli": str(fake_cli), "python": sys.executable, "version": "0"}), encoding="utf-8")
        p = subprocess.run([sys.executable, str(REPO / "herdr-plugin" / "startup.py")], env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ARGS ['herdr', 'startup']", p.stdout)

    def test_missing_cli_exits_zero_with_message(self):
        p = subprocess.run([sys.executable, str(REPO / "herdr-plugin" / "startup.py")], env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertIn("setup --only herdr", p.stderr)


if __name__ == "__main__":
    unittest.main()
