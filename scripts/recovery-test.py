#!/usr/bin/env python3
"""Explicitly disruptive recovery checks for the dedicated, single-node demo only."""
import argparse
import datetime
import json
from pathlib import Path
import time
import uuid

from verify import Verification, api, command, kubectl, port_forward


def wait_for(callback, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callback():
            return
        time.sleep(2)
    raise ValueError("Recovery condition did not become true before timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disruption", action="store_true", required=True)
    parser.add_argument("--host")
    parser.add_argument("--ca", type=Path, default=Path(".secrets/ca.crt"))
    parser.add_argument("--report", type=Path, default=Path("artifacts/recovery.json"))
    args = parser.parse_args()
    checks = Verification(args)
    checks.prerequisites()
    marker = "recovery-" + str(uuid.uuid4())
    probe = "mtc-policy-" + uuid.uuid4().hex[:8]
    node_state = {p["metadata"]["name"]: p["metadata"]["uid"]
                  for p in kubectl("-n", "observability", "get", "pvc")["items"]}

    def policy():
        service_ip = kubectl("-n", "demo", "get", "service", "demo-v1")["spec"]["clusterIP"]
        nginx_image = kubectl("-n", "demo", "get", "deployment", "demo-v1")["spec"]["template"]["spec"]["containers"][0]["image"]
        try:
            command(["kubectl", "-n", "default", "run", probe, "--restart=Never", f"--image={nginx_image}",
                     "--command", "--", "sh", "-c", f"wget -T 5 -O- http://{service_ip}:80/"])
            wait_for(lambda: kubectl("-n", "default", "get", "pod", probe).get("status", {}).get("phase") in ("Failed", "Succeeded"), 90)
            pod = kubectl("-n", "default", "get", "pod", probe)
            terminated = pod["status"]["containerStatuses"][0]["state"]["terminated"]
            output = command(["kubectl", "-n", "default", "logs", probe]).stdout
            if terminated["exitCode"] == 0 or "timed out" not in output:
                raise ValueError("Unrelated pod was not blocked specifically by a network timeout")
            if "version=v1" not in checks.request():
                raise ValueError("Allowed Gateway path stopped working")
            return {"unrelated_pod": "connection timed out", "gateway": "HTTP 200"}
        finally:
            command(["kubectl", "-n", "default", "delete", "pod", probe, "--ignore-not-found", "--wait=false"], allow_failure=True)

    def pod_recovery():
        pods = kubectl("-n", "demo", "get", "pods", "-l", "app.kubernetes.io/version=v1")["items"]
        if not pods:
            raise ValueError("No v1 pods available for recovery test")
        old = pods[0]["metadata"]
        command(["kubectl", "-n", "demo", "delete", "pod", old["name"], "--wait=true", "--timeout=90s"], timeout=100)
        command(["kubectl", "-n", "demo", "rollout", "status", "deployment/demo-v1", "--timeout=180s"], timeout=190)
        after = kubectl("-n", "demo", "get", "pods", "-l", "app.kubernetes.io/version=v1")["items"]
        if old["uid"] in {p["metadata"]["uid"] for p in after}:
            raise ValueError("Deleted pod identity was not replaced")
        checks.pods()
        checks.routes()
        return {"replaced_uid": old["uid"], "new_ready_pods": len(after)}

    def buffered_delivery():
        statefulset = kubectl("-n", "observability", "get", "statefulset", "loki")
        replicas = statefulset["spec"]["replicas"]
        if replicas != 1:
            raise ValueError("Recovery test expects exactly one Loki replica")
        identifiers = [f"{marker}-{i:03d}" for i in range(100)]
        try:
            command(["kubectl", "-n", "observability", "scale", "statefulset/loki", "--replicas=0"])
            command(["kubectl", "-n", "observability", "wait", "--for=delete", "pod/loki-0", "--timeout=120s"], timeout=130)
            for identifier in identifiers:
                checks.request(path=f"/missing-{identifier}", expected=404)
            def buffer_size():
                return int(command(["sudo", "-n", "python3", "-c",
                    "from pathlib import Path; print(sum(p.stat().st_size for p in Path('/var/lib/mtc-hack/fluentd/buffer').rglob('*.log')))"]).stdout)
            wait_for(lambda: buffer_size() > 0, 30)
            queued_bytes = buffer_size()
            command(["kubectl", "-n", "observability", "rollout", "restart", "daemonset/fluentd"])
            command(["kubectl", "-n", "observability", "rollout", "status", "daemonset/fluentd", "--timeout=180s"], timeout=190)
            if buffer_size() <= 0:
                raise ValueError("Persisted Fluentd buffer vanished while Loki was unavailable")
        finally:
            command(["kubectl", "-n", "observability", "scale", "statefulset/loki", f"--replicas={replicas}"])
            command(["kubectl", "-n", "observability", "rollout", "status", "statefulset/loki", "--timeout=300s"], timeout=310)
        with port_forward("loki", 3100) as url:
            def delivered():
                payload = api(url, "/loki/api/v1/query_range", {
                    "query": '{app="demo",namespace="demo"} |= "' + marker + '"',
                    "start": str(time.time_ns() - 30 * 60 * 10**9), "limit": "1000"})
                if payload.get("status") != "success":
                    return False
                found = set()
                for result in payload.get("data", {}).get("result", []):
                    labels = result.get("stream", {})
                    if labels.get("app") != "demo" or labels.get("namespace") != "demo":
                        continue
                    stream = labels.get("stream")
                    for _, line in result.get("values", []):
                        record = json.loads(line)
                        for identifier in identifiers:
                            path = f"/missing-{identifier}"
                            if stream == "stdout" and record.get("uri") == path and str(record.get("status")) == "404":
                                found.add((identifier, stream))
                            if stream == "stderr" and all(piece in record.get("message", "") for piece in (path, "open()", "No such file or directory")):
                                found.add((identifier, stream))
                return all((identifier, stream) in found for identifier in identifiers for stream in ("stdout", "stderr"))
            wait_for(delivered, 180)
        return {"marker": marker, "requests": len(identifiers), "delivered_stdout": 100,
                "delivered_stderr": 100, "persisted_buffer_bytes": queued_bytes,
                "fluentd_restarted_during_loki_outage": True}

    checks.check("network-policy-enforcement", policy)
    checks.check("application-pod-recovery", pod_recovery)
    checks.check("fluentd-disk-buffer-survives-restart-and-loki-outage", buffered_delivery)
    report = {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "status": "passed" if all(c["status"] == "passed" for c in checks.results) else "failed",
              "pvc_uids": node_state, "checks": checks.results}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
