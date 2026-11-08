"""Every test module imports as `tests.NAME`, the way `python -m unittest tests.NAME` runs one module on its own.
Discovery puts tests/ on the path, so a module that relies on that for its helpers passes the suite and fails for
the agent who runs it alone."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Each import starts from the interpreter's own path and modules, so one module's sys.path insert cannot cover
# another module that lacks it.
IMPORT_EACH = '''
import importlib, sys
path, modules, failed = list(sys.path), set(sys.modules), []
for name in sys.argv[1:]:
    sys.path[:] = path
    for module in set(sys.modules) - modules:
        del sys.modules[module]
    try:
        importlib.import_module('tests.' + name)
    except Exception as error:
        failed.append(f'{name}: {type(error).__name__}: {error}')
print('\\n'.join(failed))
'''


class ModulesImportAlone(unittest.TestCase):
    def test_each_module_imports_from_the_repository_root(self):
        names = sorted(path.stem for path in (ROOT / 'tests').glob('test_*.py'))
        result = subprocess.run([sys.executable, '-B', '-c', IMPORT_EACH, *names], cwd=ROOT, capture_output=True,
                                text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
