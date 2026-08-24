"""Public Screeners container and single-source configuration contract."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ScreenersPublicServiceTests(unittest.TestCase):
    def test_compose_runs_and_monitors_screeners(self) -> None:
        content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("screeners-service:", content)
        self.assertIn("algo-trader/screeners-service:", content)
        self.assertIn("screeners|http://screeners-service:8116/healthz", content)
        self.assertIn("OPTIONAL_DISCOVERY_SERVICES: audit,simulation,strategy-spec,screeners", content)

    def test_installer_pulls_screeners_image(self) -> None:
        content = (ROOT / "setup_and_run.sh").read_text(encoding="utf-8")
        self.assertIn("    screeners-service\n", content)

    def test_installer_enables_screeners_with_a_managed_gateway_secret(self) -> None:
        content = (ROOT / "setup_and_run.sh").read_text(encoding="utf-8")
        self.assertIn(
            'env_set "$SCREENERS_CANDIDATE" SCREENERS_ADMIN_PREVIEW_ENABLED true',
            content,
        )
        self.assertIn(
            'SCREENERS_GATEWAY_SHARED_SECRET="$(read_env_value "$SCREENERS_CANDIDATE" '
            'SCREENERS_GATEWAY_SHARED_SECRET)"',
            content,
        )
        self.assertIn(
            'env_set_quoted "$SCREENERS_CANDIDATE" SCREENERS_GATEWAY_SHARED_SECRET '
            '"$SCREENERS_GATEWAY_SHARED_SECRET"',
            content,
        )

    def test_watchdog_and_database_include_screeners(self) -> None:
        watchdog = (ROOT / "config/service_watchdog_public.env.example").read_text(
            encoding="utf-8"
        )
        schema = (ROOT / "algo_trader.sql").read_text(encoding="utf-8")
        self.assertIn("screeners|http://screeners-service:8116/healthz", watchdog)
        self.assertIn("CREATE TABLE IF NOT EXISTS screeners_definitions", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS screeners_runtime_state", schema)

    def test_preview_settings_have_one_configuration_source(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        example = (ROOT / "config/screeners_service.env.example").read_text(
            encoding="utf-8"
        )
        root_example = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertEqual(example.count("SCREENERS_ADMIN_PREVIEW_ENABLED="), 1)
        self.assertEqual(example.count("SCREENERS_GATEWAY_SHARED_SECRET="), 1)
        self.assertIn("SCREENERS_ADMIN_PREVIEW_ENABLED=false", example)
        self.assertNotIn("SCREENERS_ADMIN_PREVIEW_ENABLED=", root_example)
        self.assertNotIn("SCREENERS_GATEWAY_SHARED_SECRET=", root_example)

        # Backend and Screeners consume the same service env file; neither
        # duplicates the two values in its Compose environment mapping.
        self.assertGreaterEqual(
            compose.count("path: ./config/screeners_service.env.example"), 2
        )
        self.assertNotIn("SCREENERS_ADMIN_PREVIEW_ENABLED:", compose)
        self.assertNotIn("SCREENERS_GATEWAY_SHARED_SECRET:", compose)


if __name__ == "__main__":
    unittest.main()
