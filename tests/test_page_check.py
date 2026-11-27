"""Tests for skills/browser-and-computer-use/scripts/page_check.py."""

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/browser-and-computer-use/scripts'))
import page_check  # noqa: E402


class FakeBrowser:
    def __init__(self, result):
        self.result = result
        self.commands = []

    def run(self, *args, stdin=None):
        self.commands.append(args)
        if args[0] == 'eval':
            return {'result': self.result}
        return {}


class PageCheckTest(unittest.TestCase):
    def run_check(self, result):
        browser = FakeBrowser(result)
        stdout = io.StringIO()
        with mock.patch.object(page_check, 'Browser', return_value=browser), \
             mock.patch.object(sys, 'argv', ['page_check.py', '--widths', '390', 'https://example.test']), \
             contextlib.redirect_stdout(stdout):
            code = page_check.main()
        return code, stdout.getvalue(), browser.commands

    def test_clean_focusable_scroller_exits_zero(self):
        code, out, _ = self.run_check({
            'innerWidth': 390,
            'scrollWidth': 390,
            'height': 800,
            'wide': [],
            'smallCount': 0,
            'small': [],
            'wrapped': [],
            'unfocusableScrollers': [],
            'regionWrapsScroller': [],
            'scrollers': ['div.table-wrap 640/350'],
        })
        self.assertEqual(code, 0)
        self.assertIn('no problems', out)
        self.assertIn('scrolls inside: div.table-wrap 640/350', out)

    def test_unfocusable_sideways_scroller_is_a_problem(self):
        code, out, _ = self.run_check({
            'innerWidth': 390,
            'scrollWidth': 390,
            'height': 800,
            'wide': [],
            'smallCount': 0,
            'small': [],
            'wrapped': [],
            'unfocusableScrollers': ['div.table-wrap 640/350'],
            'regionWrapsScroller': [],
            'scrollers': ['div.table-wrap 640/350'],
        })
        self.assertEqual(code, 1)
        self.assertIn('1 problems', out)
        self.assertIn('scroller not keyboard-focusable (missing tabindex): div.table-wrap 640/350', out)

    def test_focusable_region_wrapping_descendant_scroller_is_a_problem(self):
        code, out, _ = self.run_check({
            'innerWidth': 390,
            'scrollWidth': 390,
            'height': 800,
            'wide': [],
            'smallCount': 0,
            'small': [],
            'wrapped': [],
            'unfocusableScrollers': [],
            'regionWrapsScroller': ['section#data wraps div.table-wrap'],
            'scrollers': ['div.table-wrap 640/350'],
        })
        self.assertEqual(code, 1)
        self.assertIn('1 problems', out)
        self.assertIn('focusable region wraps descendant scroller: section#data wraps div.table-wrap', out)

    def test_measure_script_checks_tabindex_and_role_region(self):
        self.assertIn('unfocusableScrollers', page_check.MEASURE)
        self.assertIn("hasAttribute('tabindex')", page_check.MEASURE)
        self.assertIn('[role="region"]', page_check.MEASURE)
        self.assertIn('regionWrapsScroller', page_check.MEASURE)


if __name__ == '__main__':
    unittest.main()
