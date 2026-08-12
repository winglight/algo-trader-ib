"""Fail closed if the private Screeners preview leaks into public packaging."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ScreenersPublicExclusionTests(unittest.TestCase):
    def assert_file_excludes_screeners(self, relative: str) -> None:
        path = ROOT / relative
        self.assertTrue(path.is_file(), relative)
        content = path.read_text(encoding="utf-8").lower()
        self.assertNotIn("screeners", content, relative)
        self.assertNotIn("screener", content, relative)
        self.assertNotIn("8116", content, relative)

    def test_compose_and_installers_do_not_install_or_run_screeners(self) -> None:
        for relative in (
            "docker-compose.yml",
            "middle/docker-compose.yml",
            "setup_and_run.sh",
            "scripts/install.sh",
            "scripts/install_docker.sh",
            "scripts/installer_lib.sh",
        ):
            with self.subTest(path=relative):
                self.assert_file_excludes_screeners(relative)

    def test_public_watchdog_and_service_envs_exclude_screeners(self) -> None:
        for path in sorted((ROOT / "config").glob("*.env.example")):
            with self.subTest(path=path.name):
                self.assert_file_excludes_screeners(str(path.relative_to(ROOT)))
        self.assertFalse((ROOT / "config/screeners_service.env.example").exists())

    def test_public_database_and_tree_exclude_screeners(self) -> None:
        self.assert_file_excludes_screeners("algo_trader.sql")
        forbidden_names = {
            "dockerfile.screeners",
            "start_screeners_service.sh",
            "screeners_service.env.example",
        }
        names = {path.name.lower() for path in ROOT.rglob("*") if path.is_file()}
        self.assertTrue(forbidden_names.isdisjoint(names))


if __name__ == "__main__":
    unittest.main()
