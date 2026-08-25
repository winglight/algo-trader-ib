"""Docker networking must not reserve addresses shared with other stacks."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DynamicNetworkAllocationTests(unittest.TestCase):
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
