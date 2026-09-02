"""Public Screeners container and single-source configuration contract."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ScreenersPublicServiceTests(unittest.TestCase):
    def test_installer_bypasses_stale_branch_archive_cache(self) -> None:
        installer = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")

        self.assertIn("ati_cache_bust=$(date -u +%s)", installer)
        self.assertIn('curl -fsSL "$archive_url"', installer)

    def test_runtime_env_is_ignored_and_compose_project_is_stable(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("config/*.env\n", gitignore)
        self.assertIn("config/*.env.candidate.*\n", gitignore)
        self.assertTrue(
            compose.startswith("name: ${COMPOSE_PROJECT_NAME:-ati-local-runtime}\n")
        )

    def test_compose_runs_and_monitors_screeners(self) -> None:
        content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("screeners-service:", content)
        self.assertIn("algo-trader/screeners-service:", content)
        self.assertIn("screeners|http://screeners-service:8116/healthz", content)
        self.assertIn("OPTIONAL_DISCOVERY_SERVICES: audit,simulation,strategy-spec,screeners", content)

    def test_compose_runs_and_monitors_the_licensed_audit_service(self) -> None:
        content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        audit_env = (ROOT / "config/audit_service.env.example").read_text(encoding="utf-8")

        self.assertIn("audit-service:", content)
        self.assertIn("algo-trader/audit-service:", content)
        self.assertIn("audit|http://audit-service:8109/healthz", content)
        self.assertIn("AUDIT_SERVICE_REGISTRY_HOST: audit-service", content)
        self.assertIn('AUDIT_SERVICE_REGISTRY_NAME="audit"', audit_env)

    def test_installer_pulls_screeners_image(self) -> None:
        content = (ROOT / "setup_and_run.sh").read_text(encoding="utf-8")
        self.assertIn("    screeners-service\n", content)
        self.assertIn("    audit-service\n", content)

    def test_installer_downloads_the_latest_adapter_main_source(self) -> None:
        content = (ROOT / "setup_and_run.sh").read_text(encoding="utf-8")
        self.assertIn(
            'ADAPTERS_REF="${ATI_ADAPTERS_REF:-main}"',
            content,
        )
        self.assertIn(
            'archive/refs/heads/${ADAPTERS_REF}.tar.gz',
            content,
        )
        self.assertIn("ati_cache_bust=$(date -u +%s)", content)
        self.assertNotIn("ADAPTERS_COMMIT=", content)

    def test_installer_requires_ib_screener_capability(self) -> None:
        content = (ROOT / "setup_and_run.sh").read_text(encoding="utf-8")

        self.assertIn("validate_active_adapter_contract()", content)
        self.assertIn('local tries=60', content)
        self.assertIn('capabilities.get("supports_screener") is True', content)

    def test_ib_gateway_defaults_to_scanner_filter_qualified_version(self) -> None:
        compose = (ROOT / "middle/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn(
            "${ATI_IB_GATEWAY_IMAGE:-ghcr.io/gnzsnz/ib-gateway:10.42.1a}",
            compose,
        )
        self.assertNotIn("image: ghcr.io/gnzsnz/ib-gateway:stable", compose)

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
