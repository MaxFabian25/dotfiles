import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('alignment', Path(__file__).resolve().parents[1] / 'align_with_codex.py')
alignment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alignment)


class MaintenanceSelectionTests(unittest.TestCase):
    def test_preserves_disables_uninstalls_and_unavailable_project_scopes(self):
        with tempfile.TemporaryDirectory() as project:
            installed = [
                {'id': 'pstack-plugin@codex-aligned', 'enabled': True, 'scope': 'user'},
                {'id': 'agent-browser@codex-aligned', 'enabled': False, 'scope': 'user'},
                {'id': 'interfaces@interfaces', 'enabled': True, 'scope': 'user'},
                {'id': 'nextjs@codex-aligned', 'enabled': True, 'scope': 'local', 'projectPath': project},
                {'id': 'prose@codex-aligned', 'enabled': True, 'scope': 'local', 'projectPath': project + '/removed'},
            ]
            self.assertEqual(alignment.maintenance_selection(installed), {'pstack-plugin', 'nextjs'})

    def test_unselected_catalog_entries_keep_their_existing_sources(self):
        previous = [{'name': 'pstack-plugin', 'source': './old'},
                    {'name': 'agent-browser', 'source': './disabled'},
                    {'name': 'other', 'source': './unrelated'}]
        entries = [{'name': 'pstack-plugin', 'source': './verified'}]
        actual = {entry['name']: entry['source'] for entry in alignment.merged_catalog(previous, entries)}
        self.assertEqual(actual, {'pstack-plugin': './verified', 'agent-browser': './disabled', 'other': './unrelated'})


if __name__ == '__main__':
    unittest.main()
