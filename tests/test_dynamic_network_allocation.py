"""Docker networking must not reserve addresses shared with other stacks."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DynamicNetworkAllocationTests(unittest.TestCase):
    def test_optional_compose_flags_have_warning_free_defaults(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        optional_keys = {
            "BROKER_ADAPTER_SWITCH_ENABLED",
            "BROKER_ADAPTER_SWITCH_GATE_ENABLED",
            "BROKER_ADAPTER_SWITCH_POSITION_OVERRIDE_ENABLED",
            "BROKER_ASSET_CAPABILITY_GATE_ENABLED",
            "BROKER_RUNNER_ACTIVE_ADAPTER_REDIS_KEY",
            "BROKER_RUNNER_DEFAULT_ADAPTER_ID",
            "BROKER_RUNNER_ENABLED_ADAPTERS",
            "BROKER_RUNNER_IBKR_PAPER_PROVIDER",
            "BROKER_RUNNER_PROFILE_REGISTRY_ENABLED",
            "SERVICE_WATCHDOG_MAINTENANCE_TOKEN",
        }

        unguarded = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", compose))
        self.assertTrue(optional_keys.isdisjoint(unguarded))

    def test_compose_files_do_not_assign_static_addresses(self) -> None:
        application_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        middleware_compose = (ROOT / "middle/docker-compose.yml").read_text(
            encoding="utf-8"
        )

        for compose in (application_compose, middleware_compose):
            self.assertNotIn("ipv4_address:", compose)
            self.assertNotIn("ATI_NETWORK_PREFIX", compose)

    def test_network_subnet_is_left_to_docker(self) -> None:
        middleware_compose = (ROOT / "middle/docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("ipam:", middleware_compose)
        self.assertNotIn("ATI_NETWORK_SUBNET", middleware_compose)

    def test_shared_network_is_external_and_created_idempotently(self) -> None:
        application_compose = (ROOT / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        middleware_compose = (ROOT / "middle/docker-compose.yml").read_text(
            encoding="utf-8"
        )
        installer = (ROOT / "setup_and_run.sh").read_text(encoding="utf-8")

        for compose in (application_compose, middleware_compose):
            self.assertIn("external: true", compose)
        self.assertTrue(
            middleware_compose.startswith(
                "name: ${ATI_MIDDLE_COMPOSE_PROJECT_NAME:-ati-local-middle}\n"
            )
        )
        self.assertIn(
            "name: ${ATI_MARIADB_VOLUME_NAME:-middle_mariadb-data}",
            middleware_compose,
        )
        self.assertIn("ensure_shared_network()", installer)
        self.assertIn("migrate_legacy_compose_owned_shared_network()", installer)
        self.assertIn(".shared-network-external-v1", installer)
        self.assertIn('-p "$legacy_owner"', installer)
        self.assertIn('docker rm -f "$container_id"', installer)
        self.assertIn('docker network inspect "$network_name"', installer)
        self.assertIn('docker network create "$network_name"', installer)
        self.assertLess(
            installer.index('ensure_shared_network "$(read_env_value'),
            installer.index("backup_database_for_update\n"),
        )

    def test_installer_only_persists_the_shared_network_name(self) -> None:
        installer = (ROOT / "setup_and_run.sh").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("ATI_NETWORK_NAME=stack", example)
        self.assertNotIn("ATI_NETWORK_PREFIX", example)
        self.assertNotIn("ATI_NETWORK_SUBNET", example)
        self.assertNotIn("ATI_NETWORK_PREFIX", installer)
        self.assertNotIn("ATI_NETWORK_SUBNET", installer)


if __name__ == "__main__":
    unittest.main()
