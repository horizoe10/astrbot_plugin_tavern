import ast
from pathlib import Path
import runpy
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.logger = Mock(spec=['info', 'warning', 'exception'])
        api = ModuleType('astrbot.api')
        api.logger = self.logger
        with patch.dict(sys.modules, {'astrbot.api': api}):
            self.errors = runpy.run_path(str(ROOT / 'tavern/errors.py'))

    def test_failure_categories_use_astrbot_logger(self):
        cases = (
            ('TransientError', 'transient', 'warning'),
            ('DataIntegrityError', 'integrity', 'exception'),
            ('PolicyRejection', 'policy', 'info'),
            ('EconomyDisabledError', 'policy', 'info'),
            ('InsufficientFundsError', 'policy', 'info'),
            ('EconomyConflictError', 'integrity', 'exception'),
            (None, 'unknown', 'exception'),
        )
        for error_name, category, method in cases:
            with self.subTest(error=error_name):
                self.logger.reset_mock()
                exc = self.errors[error_name]('failure') if error_name else ValueError('failure')
                try:
                    raise exc
                except Exception as caught:
                    result = self.errors['report_failure'](
                        stage='test', operation='save', exc=caught,
                        context={'session': 's1'},
                    )
                expected = f'AI 酒馆 test.save 失败（{category}） [session=s1]'
                self.assertEqual(result, expected)
                self.assertEqual(self.logger.method_calls, [
                    getattr(call, method)('%s: %s', expected, exc),
                ])

    def test_transient_override_preserves_routing(self):
        for transient, category, method in (
            (True, 'transient', 'warning'),
            (False, 'integrity', 'exception'),
        ):
            with self.subTest(transient=transient):
                self.logger.reset_mock()
                exc = self.errors['PolicyRejection']('denied')
                message = self.errors['report_failure'](
                    stage='test', operation='send', exc=exc, transient=transient,
                )
                self.assertEqual(message, f'AI 酒馆 test.send 失败（{category}）')
                self.assertEqual(self.logger.method_calls, [
                    getattr(call, method)('%s: %s', message, exc),
                ])

    def test_no_builtin_logging_in_plugin(self):
        for path in [ROOT / 'main.py', *ROOT.joinpath('tavern').rglob('*.py')]:
            with self.subTest(path=path.relative_to(ROOT)):
                tree = ast.parse(path.read_bytes())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertFalse(any(alias.name.split('.')[0] == 'logging' for alias in node.names))
                    elif isinstance(node, ast.ImportFrom):
                        self.assertNotEqual((node.module or '').split('.')[0], 'logging')
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'report_failure':
                        self.assertEqual(node.args, [], 'report_failure must use its AstrBot logger')

    def test_affected_modules_import_astrbot_logger(self):
        for relative in ('tavern/engine.py', 'tavern/errors.py', 'tavern/repositories/economy.py'):
            with self.subTest(module=relative):
                tree = ast.parse(ROOT.joinpath(relative).read_bytes())
                self.assertTrue(any(
                    isinstance(node, ast.ImportFrom) and node.module == 'astrbot.api'
                    and any(alias.name == 'logger' and alias.asname is None for alias in node.names)
                    for node in tree.body
                ))


if __name__ == '__main__':
    unittest.main()
