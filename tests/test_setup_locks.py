"""This repository's own setup: every lock matches its entry, and every source it owns matches the tree its entry
pins.

A pull stops on every machine when either is off. The other description tests load fixtures, so this file is the
only check on a pull request that changes an entry here without writing its lock anew. The private repository's
check covers the private entries the same way."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import description_toml
import drift
from description import Roots
from host_state import DeviceRoots


class PublicSetupLocks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setup = description_toml.load(Roots(ROOT / 'setup', None))

    def test_every_lock_matches_its_entry(self):
        self.assertEqual(sorted(self.setup.stale()), [], 'run lock_setup.py --entry NAME --write for each')

    def test_every_owned_source_matches_the_tree_its_entry_pins(self):
        self.assertEqual(sorted(drift.edited_owned(self.setup, DeviceRoots.current())), [],
                         'run lock_setup.py --entry NAME --refresh-owned --write for each')


if __name__ == '__main__':
    unittest.main()
