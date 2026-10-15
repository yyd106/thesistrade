"""W3 is a governed research input; its switch must change attribution."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import build, config_ops
from ashare.settings import validate_settings
from ashare.storage import Store


class MetalContextConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((Path(__file__).resolve().parents[1]/'config.example.json').read_text())

    def test_default_on_and_only_actual_booleans_are_accepted(self):
        config = copy.deepcopy(self.config)
        config.pop('global_macro_context_enabled', None)
        validate_settings(config)
        self.assertIs(config['global_macro_context_enabled'], True)
        for value in ('true', 1, None, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'global_macro_context_enabled'):
                validate_settings(dict(config, global_macro_context_enabled=value))
        validate_settings(dict(config, global_macro_context_enabled=False))

    def test_switch_is_strategy_class_and_changes_both_identity_scopes(self):
        self.assertEqual(config_ops.classify('global_macro_context_enabled'), 'STRATEGY')
        enabled = dict(self.config, global_macro_context_enabled=True)
        disabled = dict(self.config, global_macro_context_enabled=False)
        self.assertNotEqual(build.config_fingerprint(enabled), build.config_fingerprint(disabled))
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                with patch('ashare.build.behavior_runtime_fingerprint', return_value='synthetic-runtime'):
                    self.assertNotEqual(build.behavior_info(enabled, store), build.behavior_info(disabled, store))
            finally:
                store.close()
        self.assertNotIn('metal_context.py', build.DISPLAY_ONLY_MODULES)
        self.assertNotIn('global_research.py', build.DISPLAY_ONLY_MODULES)


if __name__ == '__main__': unittest.main()
