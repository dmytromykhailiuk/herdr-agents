import json
import re
import sys
import unittest

from _base import LIB, REPO

sys.path.insert(0, str(LIB))

from herdragent import __version__  # noqa: E402


class VersionsTest(unittest.TestCase):
    def test_manifests_agree(self):
        plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "herdr-agents")
        self.assertEqual(market["name"], "herdr-agents")
        self.assertEqual(plugin["version"], __version__)
        self.assertEqual(market["plugins"][0]["version"], __version__)
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])

    def test_herdr_plugin_version(self):
        toml = REPO / "herdr-plugin" / "herdr-plugin.toml"
        if not toml.exists():
            self.skipTest("herdr-plugin/herdr-plugin.toml does not exist yet")
        m = re.search(r'^version\s*=\s*"([^"]+)"', toml.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), __version__)

    def test_changelog_has_release(self):
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## {__version__}", text)


if __name__ == "__main__":
    unittest.main()
