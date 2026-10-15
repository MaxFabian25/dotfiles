from pathlib import Path
import unittest

from configuration import patch_fields, read_fields


class ConfigurationTests(unittest.TestCase):
    def test_managed_fields_preserve_local_secrets_comments_and_line_endings(self):
        before = b'\xef\xbb\xbf# local settings\r\n[tools]\r\nnode = "22" # pinned\r\n[env]\r\nLOCAL_ONLY = "not-a-real-secret"\r\n'
        fields = {('tools', 'node'): '24'}
        after = patch_fields(before, fields)
        self.assertEqual(after, before.replace(b'"22"', b'"24"'))
        self.assertEqual(patch_fields(after, fields), after)
        self.assertEqual(read_fields(after, fields), {('tools', 'node'): {'present': True, 'value': '24'}})

    def test_absent_and_false_are_distinct(self):
        fields = {('features', 'flag'): True}
        self.assertEqual(read_fields(None, fields), {('features', 'flag'): {'present': False}})
        self.assertEqual(read_fields(b'[features]\nflag = false\n', fields),
                         {('features', 'flag'): {'present': True, 'value': False}})

    def test_conflicting_structure_is_not_replaced(self):
        before = b'features = "user-owned"\n'
        with self.assertRaises(ValueError):
            patch_fields(before, {('features', 'flag'): True})


if __name__ == '__main__':
    unittest.main()
