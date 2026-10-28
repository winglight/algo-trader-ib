"""Public Compose overrides must preserve the image's Bash entrypoint."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublicManagedLauncherShellContract(unittest.TestCase):
    def test_strategy_spec_uses_bash(self):
        source = (ROOT / "docker-compose.yml").read_text()
        self.assertIn('command: ["/bin/bash", "./docker/start_strategy_spec_service.sh"]', source)
        self.assertNotIn('["/bin/sh", "./docker/start_strategy_spec_service.sh"]', source)
        self.assertNotIn('["/bin/sh", "./docker/start_screeners_service.sh"]', source)


if __name__ == "__main__":
    unittest.main()
