"""Protect the Redis-only installation identity across container recreation."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RedisIdentityPersistenceTests(unittest.TestCase):
    def test_redis_uses_aof_and_an_explicit_stable_volume_name(self) -> None:
        compose = (ROOT / "middle/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("--appendonly yes", compose)
        self.assertIn("--appendfsync everysec", compose)
        self.assertIn(
            "name: ${ATI_REDIS_VOLUME_NAME:-ati-local-runtime-redis-data}",
            compose,
        )

    def test_installer_reuses_the_volume_mounted_by_existing_redis(self) -> None:
        installer = (ROOT / "setup_and_run.sh").read_text(encoding="utf-8")
        updater = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")

        self.assertIn("resolve_redis_volume_name()", installer)
        self.assertIn("docker inspect --format", installer)
        self.assertIn('configured="${ATI_PRESERVED_REDIS_VOLUME_NAME:-}"', installer)
        self.assertIn("capture_redis_volume_name", updater)
        self.assertIn('export ATI_PRESERVED_REDIS_VOLUME_NAME="$mounted_name"', updater)
        self.assertLess(
            updater.index("capture_redis_volume_name"),
            updater.index("quiesce_runtime_for_update"),
        )
        self.assertIn('env_set "$MIDDLE_CANDIDATE" ATI_REDIS_VOLUME_NAME', installer)
        self.assertIn('env_set "$ROOT_CANDIDATE" ATI_REDIS_VOLUME_NAME', installer)


if __name__ == "__main__":
    unittest.main()
