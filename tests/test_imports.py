"""Every test module imports as `tests.NAME`, the way `python -m unittest tests.NAME` runs one module on its own.
Discovery puts tests/ on the path, so a module that relies on that for its helpers passes the suite and fails for
the agent who runs it alone."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class ModulesImportAlone(unittest.TestCase):
    def test_each_module_imports_from_the_repository_root(self):
        names = sorted(path.stem for path in (ROOT / 'tests').glob('test_*.py'))
        for name in names:
            with self.subTest(module=name):
                result = subprocess.run([sys.executable, '-B', '-c',
                                         'import importlib, sys; importlib.import_module(sys.argv[1])', f'tests.{name}'],
                                        cwd=ROOT, capture_output=True, text=True, timeout=600)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
