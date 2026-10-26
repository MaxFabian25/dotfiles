import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import tomlkit

ROOT = Path(__file__).resolve().parents[1]
MISE = shutil.which('mise')


@unittest.skipIf(MISE is None, 'mise is not on PATH')
class MiseEnvironmentTests(unittest.TestCase):
    def test_shells_keep_their_tools_while_the_selected_npm_is_not_installed(self):
        env = tomllib.loads((ROOT / 'config/mise/config.toml').read_text(encoding='utf-8'))['env']
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / '.config/mise'
            config.mkdir(parents=True)
            tools = {'npm:npm': '0.0.0'}
            (config / 'config.toml').write_text(tomlkit.dumps({'tools': tools, 'env': env}), encoding='utf-8')
            environment = {key: value for key, value in os.environ.items() if not key.startswith('MISE_')}
            environment.update(HOME=home, USERPROFILE=home, MISE_CONFIG_DIR=str(config), MISE_CEILING_PATHS=home,
                               MISE_OFFLINE='1', **{f'MISE_{name}_DIR': str(Path(home) / name.lower())
                                                    for name in ('DATA', 'STATE', 'CACHE')})
            result = subprocess.run([MISE, 'env', '--json'], cwd=home, env=environment, capture_output=True,
                                    text=True, encoding='utf-8', timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            inherited = set(environment['PATH'].split(os.pathsep))
            added = [entry for entry in json.loads(result.stdout)['PATH'].split(os.pathsep) if entry not in inherited]
            self.assertEqual([entry for entry in added if Path(entry).exists()], [])


if __name__ == '__main__':
    unittest.main()
