import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "recovery-test.py"
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("recovery_test", SOURCE)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoverySafetyTests(unittest.TestCase):
    def test_loki_restore_retries_scale_and_rollout_after_transient_failure(self):
        calls = []
        pauses = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 2:
                raise ValueError("temporary rollout failure")

        attempt = recovery.restore_loki(1, run=runner, pause=pauses.append)

        self.assertEqual(attempt, 2)
        self.assertEqual(len(calls), 4)
        self.assertIn("--request-timeout=20s", calls[0][0])
        self.assertIn("--replicas=1", calls[0][0])
        self.assertIn("rollout", calls[1][0])
        self.assertEqual(pauses, [2])

    def test_loki_restore_raises_clear_emergency_after_five_failures(self):
        calls = []
        pauses = []

        def failing_runner(args, **kwargs):
            calls.append((args, kwargs))
            raise ValueError("API unavailable")

        with self.assertRaisesRegex(
                ValueError,
                r"EMERGENCY: failed to restore Loki to 1 replica after 5 attempts"):
            recovery.restore_loki(1, run=failing_runner, pause=pauses.append)

        self.assertEqual(len(calls), 5)
        self.assertEqual(pauses, [2, 2, 2, 2])


if __name__ == "__main__":
    unittest.main()
