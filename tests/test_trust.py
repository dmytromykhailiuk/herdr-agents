import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _base import LIB  # noqa: E402

sys.path.insert(0, str(LIB))

from herdragent import trust  # noqa: E402


class TrustCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="herdr-trust-test-")))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config_dir = self.root / "cfg"
        self.config_dir.mkdir()
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir)})
        env.start()
        self.addCleanup(env.stop)
        self.parent = self.root / "parent"
        self.parent.mkdir()

    def trust(self, *paths):
        (self.config_dir / ".claude.json").write_text(json.dumps({"projects": {
            str(p): {"hasTrustDialogAccepted": True} for p in paths}}), encoding="utf-8")

    def git(self, *args):
        subprocess.run(["git", *args], check=True, capture_output=True)


class IsTrustedTest(TrustCase):
    def test_subfolder_of_trusted_folder(self):
        sub = self.parent / "a" / "b"
        sub.mkdir(parents=True)
        self.trust(self.parent)
        self.assertTrue(trust.is_trusted(str(sub)))

    def test_untrusted(self):
        self.trust(self.root / "elsewhere")
        self.assertFalse(trust.is_trusted(str(self.parent)))

    def test_nested_repo_does_not_inherit_trust(self):
        repo = self.parent / "repo"
        (repo / "src").mkdir(parents=True)
        self.git("init", "-q", str(repo))
        self.trust(self.parent)
        self.assertFalse(trust.is_trusted(str(repo)))
        self.assertFalse(trust.is_trusted(str(repo / "src")))
        self.trust(repo)
        self.assertTrue(trust.is_trusted(str(repo / "src")))

    def test_linked_worktree_uses_main_checkout(self):
        repo = self.parent / "repo"
        self.git("init", "-q", str(repo))
        self.git("-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q",
                 "--allow-empty", "-m", "init")
        wt = self.root / "wt"
        self.git("-C", str(repo), "worktree", "add", "-q", str(wt))
        self.trust(self.parent)
        self.assertFalse(trust.is_trusted(str(wt)))
        self.trust(repo)
        self.assertTrue(trust.is_trusted(str(wt)))

    def test_unreadable_config_counts_as_trusted(self):
        self.assertTrue(trust.is_trusted(str(self.parent)))


class AutoTrustCase(TrustCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"HERDR_AGENT_AUTO_TRUST": ""})
        env.start()
        self.addCleanup(env.stop)
        self.config = self.config_dir / ".claude.json"
        self.repo = self.parent / "repo"
        (self.repo / "src").mkdir(parents=True)
        self.git("init", "-q", str(self.repo))

    def write(self, data):
        self.config.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(self.config, 0o600)

    def read(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def base(self):
        return {"numStartups": 3, "projects": {
            str(self.parent): {"hasTrustDialogAccepted": True, "allowedTools": ["x"]},
            str(self.root / "other"): {"hasTrustDialogAccepted": False, "lastCost": 1.5}}}


class AutoTrustTest(AutoTrustCase):

    def test_nested_repo_under_trusted_parent(self):
        self.write(self.base())
        with mock.patch.object(trust, "log") as log:
            self.assertTrue(trust.ensure_trusted(str(self.repo / "src")))
        log.assert_called_once_with(f"auto-trusted {self.repo} (parent {self.parent} is trusted)")
        expected = self.base()
        expected["projects"][str(self.repo)] = {"hasTrustDialogAccepted": True}
        self.assertEqual(self.read(), expected)
        self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(Path(f"{self.config}.herdr-agent.bak").read_text()), self.base())
        self.assertEqual([p.name for p in self.config_dir.iterdir() if p.name.startswith(".claude.json.herdr-agent.") and p.name != ".claude.json.herdr-agent.bak"], [])

    def test_existing_entry_keeps_other_fields(self):
        data = self.base()
        data["projects"][str(self.repo)] = {"hasTrustDialogAccepted": False, "history": [1]}
        self.write(data)
        self.assertTrue(trust.ensure_trusted(str(self.repo)))
        self.assertEqual(self.read()["projects"][str(self.repo)], {"hasTrustDialogAccepted": True, "history": [1]})

    def test_opt_out(self):
        self.write(self.base())
        for value in ("off", "0", "false", "NO"):
            with mock.patch.dict(os.environ, {"HERDR_AGENT_AUTO_TRUST": value}):
                self.assertFalse(trust.ensure_trusted(str(self.repo)))
        self.assertEqual(self.read(), self.base())

    def test_no_trusted_ancestor(self):
        data = {"projects": {str(self.root / "other"): {"hasTrustDialogAccepted": True}}}
        self.write(data)
        before = self.config.read_bytes()
        self.assertFalse(trust.ensure_trusted(str(self.repo)))
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(Path(f"{self.config}.herdr-agent.bak").exists())

    def test_home_dir_does_not_count(self):
        self.write({"projects": {str(self.root): {"hasTrustDialogAccepted": True}}})
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertFalse(trust.ensure_trusted(str(self.repo)))

    def test_linked_worktree_key_is_main_checkout(self):
        self.git("-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--allow-empty", "-m", "init")
        wt = self.parent / "wt"
        self.git("-C", str(self.repo), "worktree", "add", "-q", str(wt))
        self.write(self.base())
        self.assertTrue(trust.ensure_trusted(str(wt)))
        projects = self.read()["projects"]
        self.assertTrue(projects[str(self.repo)]["hasTrustDialogAccepted"])
        self.assertNotIn(str(wt), projects)

    def test_unreadable_config_is_not_written(self):
        self.config.write_text("{not json", encoding="utf-8")
        self.assertTrue(trust.ensure_trusted(str(self.repo)))
        self.assertFalse(trust.grant_trust(str(self.config), str(self.repo)))
        self.assertEqual(self.config.read_text(encoding="utf-8"), "{not json")
        self.assertFalse(Path(f"{self.config}.herdr-agent.bak").exists())

    def test_concurrent_change_retries(self):
        self.write(self.base())
        real_dump = json.dump
        calls = []

        def dump(data, f, **kw):
            if not calls:
                changed = self.base()
                changed["numStartups"] = 99
                self.config.write_text(json.dumps(changed, indent=4), encoding="utf-8")
            calls.append(1)
            real_dump(data, f, **kw)

        with mock.patch.object(trust.json, "dump", dump):
            self.assertTrue(trust.grant_trust(str(self.config), str(self.repo)))
        self.assertEqual(len(calls), 2)
        data = self.read()
        self.assertEqual(data["numStartups"], 99)
        self.assertTrue(data["projects"][str(self.repo)]["hasTrustDialogAccepted"])


class InheritMcpTest(AutoTrustCase):
    def settings(self, folder, name="settings.local.json"):
        return folder / ".claude" / name

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def target(self):
        return json.loads(self.settings(self.repo).read_text(encoding="utf-8"))

    def test_inherits_from_parent_project_settings(self):
        self.write(self.base())
        self.put(self.settings(self.parent, "settings.json"),
                 {"enableAllProjectMcpServers": True, "enabledMcpjsonServers": ["context7"], "model": "x"})
        with mock.patch.object(trust, "log") as log:
            self.assertTrue(trust.ensure_trusted(str(self.repo / "src")))
        log.assert_called_with(f"inherited MCP approvals for {self.repo} from {self.parent}")
        self.assertEqual(self.target(), {"enableAllProjectMcpServers": True, "enabledMcpjsonServers": ["context7"]})
        self.assertEqual([p.name for p in self.settings(self.repo).parent.iterdir()], ["settings.local.json"])

    def test_union_without_duplicates_and_explicit_target_kept(self):
        data = self.base()
        data["projects"][str(self.parent)]["disabledMcpjsonServers"] = ["legacy-off"]
        self.write(data)
        self.put(self.settings(self.parent), {"enabledMcpjsonServers": ["a", "b"], "enableAllProjectMcpServers": True})
        self.put(self.settings(self.parent, "settings.json"), {"enabledMcpjsonServers": ["b", "c"]})
        self.put(self.settings(self.repo), {"enabledMcpjsonServers": ["b", "z"], "enableAllProjectMcpServers": False,
                                            "permissions": {"allow": ["Bash"]}})
        self.assertTrue(trust.ensure_trusted(str(self.repo)))
        self.assertEqual(self.target(), {"enabledMcpjsonServers": ["b", "z", "a", "c"], "enableAllProjectMcpServers": False,
                                         "permissions": {"allow": ["Bash"]}, "disabledMcpjsonServers": ["legacy-off"]})

    def test_nothing_written_without_parent_decision(self):
        self.write(self.base())
        self.put(self.settings(self.parent, "settings.json"), {"model": "x", "enabledMcpjsonServers": []})
        with mock.patch.object(trust, "log") as log:
            self.assertTrue(trust.ensure_trusted(str(self.repo)))
        log.assert_called_once_with(f"auto-trusted {self.repo} (parent {self.parent} is trusted)")
        self.assertFalse((self.repo / ".claude").exists())

    def test_opt_out_writes_nothing(self):
        self.write(self.base())
        self.put(self.settings(self.parent), {"enabledMcpjsonServers": ["context7"]})
        with mock.patch.dict(os.environ, {"HERDR_AGENT_AUTO_TRUST": "off"}):
            self.assertFalse(trust.ensure_trusted(str(self.repo)))
        self.assertFalse((self.repo / ".claude").exists())

    def test_already_trusted_is_not_touched(self):
        data = self.base()
        data["projects"][str(self.repo)] = {"hasTrustDialogAccepted": True}
        self.write(data)
        self.put(self.settings(self.parent), {"enabledMcpjsonServers": ["context7"]})
        self.assertTrue(trust.ensure_trusted(str(self.repo)))
        self.assertFalse((self.repo / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
