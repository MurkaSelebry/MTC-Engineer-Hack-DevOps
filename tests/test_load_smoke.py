"""Budget guards and measured-result aggregation for the optional load smoke."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'load-smoke.py'
spec = importlib.util.spec_from_file_location('load_smoke', SOURCE)
smoke = importlib.util.module_from_spec(spec) if SOURCE.exists() else None
if smoke:
    spec.loader.exec_module(smoke)


class LoadSmokeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(smoke, 'load smoke implementation must exist')

    def test_cli_rejects_invalid_hosts_and_unbounded_budgets(self):
        invalid = [[], ['--host', 'demo.test'], ['--host', '::1'], ['--host', '127.0.0.1', '--requests', '0'],
                   ['--host', '127.0.0.1', '--requests', '10001'], ['--host', '127.0.0.1', '--concurrency', '0'],
                   ['--host', '127.0.0.1', '--concurrency', '51']]
        for arguments in invalid:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                smoke.parse_args(arguments)
            self.assertEqual(error.exception.code, 2)
        args = smoke.parse_args(['--host', '192.0.2.1'])
        self.assertEqual((args.requests, args.concurrency), (1000, 10))

    def test_aggregation_computes_percentiles_and_fails_on_errors_or_unstarted_requests(self):
        results = [{'ok': True, 'seconds': seconds} for seconds in (.01, .02, .03, .04)]
        report = smoke.summarize(results, requested=4, wall_seconds=2)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['errors'], 0)
        self.assertEqual(report['wall_rps'], 2)
        self.assertEqual(report['latency_ms'], {'p50': 20, 'p95': 40, 'p99': 40, 'max': 40})
        results[0] = {'ok': False, 'seconds': .01, 'error': 'unexpected HTTP status'}
        report = smoke.summarize(results, requested=5, wall_seconds=2)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['errors'], 2)
        self.assertEqual(report['not_started'], 1)
        self.assertEqual(report['successful'], 3)


if __name__ == '__main__':
    unittest.main()
