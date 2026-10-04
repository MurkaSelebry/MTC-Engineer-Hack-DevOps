"""Acceptance rules exercise real parser/validator behavior, never fake cluster success."""
import importlib.util
import pathlib
import re
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify.py"
spec = importlib.util.spec_from_file_location("verify", SOURCE)
verify = importlib.util.module_from_spec(spec) if SOURCE.exists() else None
if verify:
    spec.loader.exec_module(verify)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(verify, "verification implementation must exist")

    def test_stale_gateway_conditions_fail(self):
        conditions = [{"type": "Accepted", "status": "True", "observedGeneration": 1}]
        with self.assertRaises(ValueError):
            verify.require_conditions(conditions, ["Accepted"], 2)

    def test_missing_and_false_conditions_fail(self):
        for conditions in ([], [{"type": "Accepted", "status": "False"}]):
            with self.assertRaises(ValueError):
                verify.require_conditions(conditions, ["Accepted"], 1)

    def test_current_conditions_pass(self):
        verify.require_conditions([{"type": "Accepted", "status": "True", "observedGeneration": 2}], ["Accepted"], 2)

    def test_unready_and_empty_pods_fail(self):
        for pods in ([], [{"metadata": {"name": "broken"}, "status": {"phase": "Running", "conditions": []}}]):
            with self.assertRaises(ValueError):
                verify.require_ready_pods(pods)

    def test_ready_pod_passes(self):
        verify.require_ready_pods([{"metadata": {"name": "app"}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}])

    def test_prometheus_error_and_nonfinite_samples_fail(self):
        for payload in ({"status": "error"}, {"status": "success", "data": {"result": []}}, {"status": "success", "data": {"result": [{"value": [1, "NaN"]}]}}):
            with self.assertRaises(ValueError):
                verify.prom_value(payload)

    def test_counter_must_increase_by_requested_amount(self):
        with self.assertRaises(ValueError):
            verify.require_counter_growth(100, 105, 10)
        with self.assertRaises(ValueError):
            verify.require_counter_growth(100, 2, 10)
        verify.require_counter_growth(100, 120, 10)

    def log_payload(self, stream, record):
        return {"status": "success", "data": {"result": [{"stream": {"stream": stream, "app": "demo", "namespace": "demo"}, "values": [["1", json.dumps(record)]]}]}}

    def test_loki_requires_actual_successful_access_record(self):
        payload = self.log_payload("stdout", {"uri": "/?verify=abc", "status": 200})
        self.assertTrue(verify.loki_contains(payload, "abc", "stdout"))
        self.assertFalse(verify.loki_contains(payload, "abc", "stderr"))
        self.assertFalse(verify.loki_contains(payload, "missing", "stdout"))
        self.assertFalse(verify.loki_contains(self.log_payload("stdout", {"uri": "/missing-abc", "status": 404}), "abc", "stdout"))

    def test_loki_rejects_mislabelled_access_as_error_evidence(self):
        payload = self.log_payload("stderr", {"uri": "/?verify=abc", "status": 200})
        self.assertFalse(verify.loki_contains(payload, "abc", "stderr"))
        self.assertFalse(verify.loki_contains(self.log_payload("stderr", {"message": "unrelated abc"}), "abc", "stderr"))

    def test_loki_requires_nginx_missing_file_error(self):
        message = '2026/10/04 [error] open() "/usr/share/nginx/html/missing-abc" failed (2: No such file or directory), request: "GET /missing-abc HTTP/1.1"'
        self.assertTrue(verify.loki_contains(self.log_payload("stderr", {"message": message}), "abc", "stderr"))
        self.assertFalse(verify.loki_contains(self.log_payload("stderr", {"message": message}), "different", "stderr"))

    def test_rollout_rejects_old_replicas_and_stale_daemonset_generation(self):
        self.assertTrue(hasattr(verify, "require_rollout"), "rollout freshness validator required")
        deployment = {"kind": "Deployment", "metadata": {"name": "demo-v1", "generation": 2}, "spec": {"replicas": 2}, "status": {"observedGeneration": 2, "replicas": 2, "updatedReplicas": 0, "readyReplicas": 2, "availableReplicas": 2}}
        with self.assertRaises(ValueError):
            verify.require_rollout(deployment)
        daemonset = {"kind": "DaemonSet", "metadata": {"name": "fluentd", "generation": 2}, "status": {"observedGeneration": 1, "desiredNumberScheduled": 1, "updatedNumberScheduled": 1, "numberReady": 1, "numberAvailable": 1}}
        with self.assertRaises(ValueError):
            verify.require_rollout(daemonset)
        deployment["status"]["updatedReplicas"] = 2
        daemonset["status"]["observedGeneration"] = 2
        verify.require_rollout(deployment)
        verify.require_rollout(daemonset)
        daemonset["status"]["updatedNumberScheduled"] = 0
        with self.assertRaises(ValueError):
            verify.require_rollout(daemonset)

    def test_scrapes_must_cover_every_ready_envoy_pod(self):
        self.assertTrue(hasattr(verify, "require_envoy_targets"), "target coverage validator required")
        pods = [{"metadata": {"name": f"envoy-{i}"}, "status": {"podIP": f"10.244.0.{i}", "phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}} for i in (1, 2)]
        targets = [{"scrapeUrl": f"http://10.244.0.{i}:19001/stats/prometheus", "labels": {"namespace": "envoy-gateway-system", "instance": f"10.244.0.{i}:19001"}, "health": "up"} for i in (1, 2)]
        with self.assertRaises(ValueError):
            verify.require_envoy_targets(pods, targets[:1])
        self.assertEqual(len(verify.require_envoy_targets(pods, targets)), 2)
        targets[1]["scrapeUrl"] = "http://10.244.0.99:19001/stats/prometheus"
        with self.assertRaises(ValueError):
            verify.require_envoy_targets(pods, targets)
        targets[1]["scrapeUrl"] = "http://10.244.0.2:19001/stats/prometheus"
        targets[1]["health"] = "down"
        with self.assertRaises(ValueError):
            verify.require_envoy_targets(pods, targets)

    def selector_pattern(self, selector, label):
        match = re.search(re.escape(label) + r'=~("(?:\\.|[^"\\])*")', selector)
        self.assertIsNotNone(match, f"required traffic filter {label} is absent")
        return json.loads(match.group(1))

    def test_upstream_counter_excludes_scrape_and_control_plane_requests(self):
        self.assertTrue(hasattr(verify, "envoy_counter_selector"), "traffic-scoped selector required")
        selector = verify.envoy_counter_selector("envoy_cluster_upstream_rq_total", ["10.244.0.1:19001"])
        pattern = self.selector_pattern(selector, "envoy_cluster_name")
        for cluster in ("prometheus_stats", "xds_cluster", "admin", "httproute/other/demo/rule/1"):
            self.assertIsNone(re.fullmatch(pattern, cluster), cluster)
        for cluster in ("httproute/demo/demo/rule/0", "httproute/demo/demo/rule/1", "httproute/demo/canary/rule/0"):
            self.assertIsNotNone(re.fullmatch(pattern, cluster), cluster)
        instance_pattern = self.selector_pattern(selector, "instance")
        self.assertIsNotNone(re.fullmatch(instance_pattern, "10.244.0.1:19001"))
        self.assertIsNone(re.fullmatch(instance_pattern, "10x244x0x1:19001"))

    def test_downstream_counter_excludes_admin_stats_and_readiness_listeners(self):
        self.assertTrue(hasattr(verify, "envoy_counter_selector"), "traffic-scoped selector required")
        selector = verify.envoy_counter_selector("envoy_http_downstream_rq_total", ["10.244.0.1:19001"])
        pattern = self.selector_pattern(selector, "envoy_http_conn_manager_prefix")
        for listener in ("admin", "eg-ready-http", "eg-stats-http"):
            self.assertIsNone(re.fullmatch(pattern, listener), listener)
        for listener in ("http-10080", "https-10443"):
            self.assertIsNotNone(re.fullmatch(pattern, listener), listener)

    def test_fresh_metric_baseline_rejects_previous_canary_scrape(self):
        self.assertTrue(hasattr(verify, "require_fresh_samples"), "scrape freshness validator required")
        payload = {"status": "success", "data": {"result": [{"metric": {"instance": "envoy-a"}, "value": [200, "90"]}, {"metric": {"instance": "envoy-b"}, "value": [200, "110"]}]}}
        with self.assertRaises(ValueError):
            verify.require_fresh_samples(payload, ["envoy-a", "envoy-b"], 100)
        payload["data"]["result"][0]["value"][1] = "111"
        self.assertEqual(verify.require_fresh_samples(payload, ["envoy-a", "envoy-b"], 100), {"envoy-a": 111.0, "envoy-b": 110.0})
        payload["data"]["result"].pop()
        with self.assertRaises(ValueError):
            verify.require_fresh_samples(payload, ["envoy-a", "envoy-b"], 100)

    def test_canary_rejects_all_one_version_and_unknown_body(self):
        for samples in (["Hello World! version=v1\n"] * 400, ["oops"] * 400):
            with self.assertRaises(ValueError):
                verify.canary_distribution(samples, False)

    def test_canary_accepts_90_10_distribution(self):
        result = verify.canary_distribution(["Hello World! version=v1\n"] * 900 + ["Hello World! version=v2\n"] * 100, False)
        self.assertEqual(result["v2"], 100)

    def test_canary_full_rejects_small_or_20_percent_samples(self):
        for samples in (["Hello World! version=v1\n"] * 360 + ["Hello World! version=v2\n"] * 40,
                        ["Hello World! version=v1\n"] * 800 + ["Hello World! version=v2\n"] * 200):
            with self.assertRaises(ValueError):
                verify.canary_distribution(samples, False)

    def test_canary_requires_meaningful_sample(self):
        with self.assertRaises(ValueError):
            verify.canary_distribution(["Hello World! version=v1\n"] * 9 + ["Hello World! version=v2\n"], False)

    def test_missing_tools_produce_failure_reports_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            report = pathlib.Path(directory) / "report.json"
            junit = pathlib.Path(directory) / "report.xml"
            result = subprocess.run([sys.executable, str(SOURCE), "--quick", "--report", str(report), "--junit", str(junit)], env=dict(os.environ, PATH=directory), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            payload = json.loads(report.read_text())
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["checks"][0]["name"], "prerequisites")
            self.assertIn("required command not found", payload["checks"][0]["error"])
            self.assertEqual(ET.parse(junit).getroot().get("failures"), "1")
            self.assertNotIn("passed", result.stdout.lower())

    def test_prom_value_parses_sum(self):
        self.assertEqual(verify.prom_value({"status": "success", "data": {"result": [{"value": [1, "123.5"]}]}}), 123.5)


if __name__ == "__main__":
    unittest.main()
