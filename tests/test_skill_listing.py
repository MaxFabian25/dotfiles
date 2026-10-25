from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import skill_listing


class VerdictTest(unittest.TestCase):
    def test_an_over_budget_listing_names_the_overflow(self):
        code, message = skill_listing.verdict(
            '[DEBUG] Skill listing over budget: 118 skills, 61418 chars > 30000 budget\n'
            '[DEBUG] Sending 118 skills via attachment (initial)\n')
        self.assertEqual(code, 1)
        self.assertIn('61418 chars > 30000 budget', message)

    def test_a_listing_within_budget_passes(self):
        self.assertEqual(skill_listing.verdict('[DEBUG] Sending 124 skills via attachment (initial)\n'),
                         (0, 'Claude Code lists all 124 skills with their descriptions.'))

    def test_a_log_without_a_listing_is_inconclusive(self):
        self.assertEqual(skill_listing.verdict('')[0], 2)


if __name__ == '__main__':
    unittest.main()
