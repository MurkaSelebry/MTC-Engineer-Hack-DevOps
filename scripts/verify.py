#!/usr/bin/env python3
"""Bounded acceptance checks against a real cluster; no third-party Python packages."""
import argparse
import contextlib
import datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET


BODIES = {v: f"Hello World! version={v}\n" for v in ("v1", "v2")}
DEFAULT_POD_CIDR = "10.244.0.0/16"


def require_conditions(conditions, expected, generation):
    for name in expected:
        found = next((c for c in conditions if c.get("type") == name), {})
        if found.get("status") != "True" or found.get("observedGeneration", -1) < generation:
            raise ValueError(f"condition {name} is missing, false, or stale")


def require_ready_pods(pods):
    if not pods:
        raise ValueError("no pods found")
    for pod in pods:
        status = pod.get("status", {})
        if status.get("phase") == "Succeeded":
            continue
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
        if pod.get("metadata", {}).get("deletionTimestamp") or status.get("phase") != "Running" or not ready:
            raise ValueError(f"pod {pod['metadata']['name']} is not Running and Ready")


def require_cluster_network(nodes, pods, coredns_pods, calico_pods, pod_cidr):
    expected = ipaddress.ip_network(pod_cidr)
    if not nodes:
        raise ValueError("cluster has no nodes")
    for node in nodes:
        metadata, status = node.get("metadata", {}), node.get("status", {})
        name = metadata.get("name", "unnamed")
        ready = next((condition for condition in status.get("conditions", [])
                      if condition.get("type") == "Ready"), {})
        if metadata.get("deletionTimestamp") or ready.get("status") != "True":
            raise ValueError(f"node {name} is not Ready")
        configured = node.get("spec", {}).get("podCIDRs", [])
        if not configured and node.get("spec", {}).get("podCIDR"):
            configured = [node["spec"]["podCIDR"]]
        if not configured:
            raise ValueError(f"node {name} has no assigned Pod CIDR")
        for cidr in configured:
            network = ipaddress.ip_network(cidr)
            if network.version != expected.version or not network.subnet_of(expected):
                raise ValueError(f"node Pod CIDR {network} is outside expected Pod CIDR {expected}")

    require_ready_pods(coredns_pods)
    require_ready_pods(calico_pods)
    checked = 0
    for pod in pods:
        if pod.get("spec", {}).get("hostNetwork"):
            continue
        status = pod.get("status", {})
        if status.get("phase") in ("Succeeded", "Failed"):
            continue
        metadata = pod.get("metadata", {})
        identity = f"{metadata.get('namespace', 'default')}/{metadata.get('name', 'unnamed')}"
        addresses = [item.get("ip") for item in status.get("podIPs", []) if item.get("ip")]
        if not addresses and status.get("podIP"):
            addresses = [status["podIP"]]
        if not addresses:
            raise ValueError(f"pod {identity} has no pod IP")
        for address in addresses:
            if ipaddress.ip_address(address) not in expected:
                raise ValueError(f"pod {identity} IP {address} is outside expected Pod CIDR {expected}")
        checked += 1
    if not checked:
        raise ValueError("no active non-host-network pods found")
    return {"nodes": len(nodes), "pod_cidr": str(expected), "checked_non_host_pods": checked,
            "coredns_pods": len(coredns_pods), "calico_pods": len(calico_pods)}


def prom_value(payload):
    if payload.get("status") != "success":
        raise ValueError("Prometheus query failed")
    result = payload.get("data", {}).get("result", [])
    if len(result) != 1:
        raise ValueError("Prometheus sum query must return one sample")
    value = float(result[0]["value"][1])
    if not math.isfinite(value):
        raise ValueError("Prometheus sample is not finite")
    return value


def require_counter_growth(before, after, minimum):
    if not math.isfinite(before) or not math.isfinite(after) or after - before < minimum:
        raise ValueError(f"request counter grew {after - before:g}; expected at least {minimum}")


def require_rollout(workload):
    metadata, status = workload["metadata"], workload.get("status", {})
    if status.get("observedGeneration", -1) < metadata["generation"]:
        raise ValueError(f"{metadata['name']} rollout status is stale")
    if workload["kind"] == "Deployment":
        desired = workload["spec"].get("replicas", 1)
        fields = ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
    elif workload["kind"] == "DaemonSet":
        desired = status.get("desiredNumberScheduled", 0)
        fields = ("updatedNumberScheduled", "numberReady", "numberAvailable")
    else:
        raise ValueError("unsupported workload kind")
    if desired < 1 or any(status.get(field, 0) != desired for field in fields):
        raise ValueError(f"{metadata['name']} rollout has old, missing, or unavailable replicas")


def require_envoy_targets(pods, targets):
    require_ready_pods(pods)
    expected = {pod.get("status", {}).get("podIP") for pod in pods}
    if None in expected or len(expected) != len(pods):
        raise ValueError("Envoy pods require distinct pod IPs")
    envoy = []
    covered = set()
    for target in targets:
        parsed = urllib.parse.urlsplit(target.get("scrapeUrl", ""))
        namespace = target.get("labels", {}).get("namespace") or target.get("discoveredLabels", {}).get("__meta_kubernetes_namespace")
        if parsed.port != 19001 or namespace != "envoy-gateway-system":
            continue
        if parsed.path != "/stats/prometheus" or target.get("health") != "up":
            raise ValueError("Envoy metrics target is unhealthy or has the wrong scrape path")
        if not target.get("labels", {}).get("instance"):
            raise ValueError("Envoy metrics target is missing its instance label")
        covered.add(parsed.hostname)
        envoy.append(target)
    if covered != expected:
        raise ValueError(f"Envoy scrape coverage differs from live pods: expected {len(expected)}, covered {len(covered)}, missing {len(expected - covered)}")
    return envoy


def envoy_counter_selector(name, instances):
    matcher = json.dumps("|".join(re.escape(instance) for instance in instances))
    if name.startswith("envoy_") and name.endswith("upstream_rq_total"):
        # Real Envoy metrics also contain prometheus_stats and xds_cluster.
        # Only these HTTPRoute clusters carry the application's requests.
        traffic = 'envoy_cluster_name=~"httproute/demo/(demo|canary)/rule/.*"'
    elif name.startswith("envoy_") and name.endswith("downstream_rq_total"):
        # Data-plane listener prefixes verified on the generated Envoy Pods.
        traffic = 'envoy_http_conn_manager_prefix=~"http-10080|https-10443"'
    else:
        raise ValueError("unsupported Envoy request counter")
    return f'{name}{{instance=~{matcher},{traffic}}}'


def require_fresh_samples(payload, instances, barrier):
    # Values must come from timestamp(metric), not the instant-query evaluation time.
    if payload.get("status") != "success":
        raise ValueError("Prometheus timestamp query failed")
    samples = {}
    for result in payload.get("data", {}).get("result", []):
        instance = result.get("metric", {}).get("instance")
        timestamp = float(result["value"][1])
        if not math.isfinite(timestamp) or timestamp < barrier or instance in samples:
            raise ValueError("Envoy counter has not been scraped after the traffic barrier")
        samples[instance] = timestamp
    if set(samples) != set(instances):
        raise ValueError("fresh counter samples are missing for one or more Envoy targets")
    return samples


def loki_contains(payload, marker, stream):
    if payload.get("status") != "success":
        return False
    for result in payload.get("data", {}).get("result", []):
        labels = result.get("stream", {})
        if not all(labels.get(k) == v for k, v in {"app": "demo", "namespace": "demo", "stream": stream}.items()):
            continue
        for _, line in result.get("values", []):
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            if stream == "stdout" and record.get("uri") == f"/?verify={marker}" and str(record.get("status")) == "200":
                return True
            message = record.get("message", "")
            if stream == "stderr" and isinstance(message, str) and all(fragment in message for fragment in (f"/missing-{marker}", "open()", "No such file or directory")):
                return True
    return False


def canary_distribution(samples, quick):
    minimum, low, high = (400, .04, .18) if quick else (1000, .05, .15)
    if len(samples) < minimum:
        raise ValueError(f"canary requires at least {minimum} requests")
    if any(body not in BODIES.values() for body in samples):
        raise ValueError("canary returned an unknown body")
    counts = {version: samples.count(body) for version, body in BODIES.items()}
    fraction = counts["v2"] / len(samples)
    if not low <= fraction <= high:
        raise ValueError(f"canary v2 proportion {fraction:.3f} outside [{low}, {high}]")
    return dict(counts, samples=len(samples), v2_fraction=fraction, accepted_range=[low, high])


def command(args, timeout=30, allow_failure=False):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{Path(args[0]).name} timed out after {timeout}s") from exc
    if result.returncode and not allow_failure:
        # Never copy arbitrary CLI stderr (which could include configuration secrets).
        raise ValueError(f"{Path(args[0]).name} failed with exit {result.returncode}")
    return result


def kubectl(*args):
    return json.loads(command(["kubectl", "--request-timeout=20s", *args, "-o", "json"]).stdout)


def api(base, path, params=None):
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


@contextlib.contextmanager
def port_forward(service, remote):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(["kubectl", "--request-timeout=20s", "-n", "observability", "port-forward", "--address=127.0.0.1", f"service/{service}", f"{port}:{remote}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError(f"port-forward for {service} exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(.2)
        else:
            raise ValueError(f"port-forward for {service} did not become ready")
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class Verification:
    def __init__(self, args):
        self.args = args
        self.results = []
        self.host = args.host or os.environ.get("VM_IP")
        self.pod_cidr = getattr(args, "pod_cidr", DEFAULT_POD_CIDR)

    def check(self, name, callback):
        start = time.monotonic()
        try:
            details = callback()
            result = {"name": name, "status": "passed", "details": details}
        except (ValueError, OSError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as exc:
            # HTTP/JSON errors and messages are controlled; do not dump subprocess output.
            result = {"name": name, "status": "failed", "error": str(exc)[:500]}
        result["seconds"] = round(time.monotonic() - start, 3)
        self.results.append(result)
        print(f"{result['status'].upper()}: {name}" + (f": {result['error']}" if "error" in result else ""), flush=True)
        return result["status"] == "passed"

    def prerequisites(self):
        for binary in ("kubectl", "curl", "openssl"):
            if not shutil.which(binary):
                raise ValueError(f"required command not found: {binary}")
        if not self.args.ca.is_file():
            raise ValueError(f"CA certificate not found: {self.args.ca}")
        nodes = kubectl("get", "nodes")["items"]
        if not nodes:
            raise ValueError("cluster has no nodes")
        if not self.host:
            self.host = next(a["address"] for a in nodes[0]["status"]["addresses"] if a["type"] == "InternalIP")
        # curl --resolve needs an IP. Resolve a supplied DNS host once.
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            self.host = socket.gethostbyname(self.host)
        return {"host": self.host, "nodes": len(nodes)}

    def pods(self):
        counts = {}
        for namespace in ("demo", "envoy-gateway-system", "observability"):
            pods = kubectl("-n", namespace, "get", "pods")["items"]
            require_ready_pods(pods)
            counts[namespace] = len(pods)
        for version in ("v1", "v2"):
            deployment = kubectl("-n", "demo", "get", "deployment", f"demo-{version}")
            require_rollout(deployment)
        daemonsets = kubectl("-n", "observability", "get", "daemonsets", "-l", "app.kubernetes.io/name=fluentd")["items"]
        if not daemonsets:
            raise ValueError("Fluentd DaemonSet not found")
        for ds in daemonsets:
            require_rollout(ds)
        return counts

    def cluster_network(self):
        nodes = kubectl("get", "nodes")["items"]
        pods = kubectl("get", "pods", "-A")["items"]
        coredns = kubectl("-n", "kube-system", "get", "pods", "-l", "k8s-app=kube-dns")["items"]
        calico = kubectl("-n", "calico-system", "get", "pods")["items"]
        return require_cluster_network(nodes, pods, coredns, calico, self.pod_cidr)

    def gateway(self):
        gateway = kubectl("-n", "demo", "get", "gateway", "demo")
        generation = gateway["metadata"]["generation"]
        require_conditions(gateway.get("status", {}).get("conditions", []), ["Accepted", "Programmed"], generation)
        listeners = gateway.get("status", {}).get("listeners", [])
        if len(listeners) < 2:
            raise ValueError("Gateway requires HTTP and HTTPS listener status")
        for listener in listeners:
            require_conditions(listener.get("conditions", []), ["Accepted", "Programmed", "ResolvedRefs"], generation)
        routes = kubectl("-n", "demo", "get", "httproutes")["items"]
        hostnames = set()
        for route in routes:
            parents = [p for p in route.get("status", {}).get("parents", []) if p.get("parentRef", {}).get("name") == "demo"]
            if not parents:
                continue
            for parent in parents:
                require_conditions(parent.get("conditions", []), ["Accepted", "ResolvedRefs"], route["metadata"]["generation"])
            hostnames.update(route.get("spec", {}).get("hostnames", []))
        if not {"demo.test", "canary.test"} <= hostnames:
            raise ValueError("accepted routes for demo.test and canary.test are required")
        return {"listeners": len(listeners), "route_hosts": sorted(hostnames)}

    def request(self, hostname="demo.test", path="/", tls=False, ca=None, expected=200):
        port = 30443 if tls else 30080
        address = f"[{self.host}]" if ":" in self.host else self.host
        args = ["curl", "--silent", "--show-error", "--noproxy", "*", "--connect-timeout", "5", "--max-time", "10", "--resolve", f"{hostname}:{port}:{address}", "--write-out", "\n%{http_code}"]
        if tls:
            args += ["--cacert", str(ca or self.args.ca)]
        args += [f"{'https' if tls else 'http'}://{hostname}:{port}{path}"]
        result = command(args, timeout=15, allow_failure=True)
        if result.returncode:
            raise ValueError(f"curl failed with exit {result.returncode}")
        body, code = result.stdout.rsplit("\n", 1)
        if code != str(expected):
            raise ValueError(f"{hostname}{path}: HTTP {code}, expected {expected}")
        return body

    def routes(self):
        for tls in (False, True):
            for path, version in (("/", "v1"), ("/v2", "v2"), ("/v2/", "v2")):
                if self.request(path=path, tls=tls) != BODIES[version]:
                    raise ValueError(f"route {path} returned wrong body (TLS={tls})")
            if self.request(hostname="canary.test", tls=tls) not in BODIES.values():
                raise ValueError("canary hostname returned wrong body")
        return {"protocols": ["http", "https"], "paths": ["/", "/v2", "/v2/"]}

    def tls_negative(self):
        # A valid unrelated CA tests trust, while correct CA + wrong host tests identity.
        with tempfile.TemporaryDirectory(prefix="mtc-verify-") as directory:
            wrong_ca = Path(directory) / "unrelated.crt"
            command(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(Path(directory) / "temporary.key"), "-out", str(wrong_ca), "-days", "1", "-subj", "/CN=Unrelated verification CA"], timeout=20)
            for host, ca in (("wrong.invalid", self.args.ca), ("demo.test", wrong_ca)):
                try:
                    self.request(hostname=host, tls=True, ca=ca)
                except ValueError as exc:
                    if str(exc) != "curl failed with exit 60":
                        raise ValueError(f"TLS negative case {host} failed for a reason other than certificate validation") from exc
                else:
                    raise ValueError(f"TLS unexpectedly accepted {host}")
        return {"wrong_hostname": "certificate rejected", "wrong_ca": "certificate rejected"}

    def canary(self):
        samples = []
        deadline = time.monotonic() + (180 if self.args.quick else 300)
        for _ in range(400 if self.args.quick else 1000):
            if time.monotonic() > deadline:
                raise ValueError("canary request sampling exceeded its time limit")
            samples.append(self.request(hostname="canary.test"))
        return canary_distribution(samples, self.args.quick)

    def service(self, preferred, selector, port):
        services = kubectl("-n", "observability", "get", "services")["items"]
        matches = [s for s in services if any(p.get("port") == port for p in s["spec"].get("ports", [])) and all(s["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())]
        preferred_matches = [s for s in services if s["metadata"]["name"] == preferred and any(p.get("port") == port for p in s["spec"].get("ports", []))]
        candidates = preferred_matches or matches
        if len(candidates) != 1:
            raise ValueError(f"cannot uniquely discover {preferred} service")
        return candidates[0]["metadata"]["name"]

    def metrics(self):
        service = self.service("monitoring-kube-prometheus-prometheus", {"app.kubernetes.io/name": "prometheus"}, 9090)
        pods = kubectl("-n", "envoy-gateway-system", "get", "pods", "-l", "app.kubernetes.io/component=proxy,app.kubernetes.io/managed-by=envoy-gateway")["items"]
        with port_forward(service, 9090) as base:
            targets = api(base, "/api/v1/targets").get("data", {}).get("activeTargets", [])
            envoy = require_envoy_targets(pods, targets)
            instances = sorted({t["labels"]["instance"] for t in envoy})
            names = api(base, "/api/v1/label/__name__/values").get("data", [])
            names = [n for n in names if re.fullmatch(r"envoy_.*(?:downstream_rq_total|upstream_rq_total)", n)]
            if not names:
                raise ValueError("no real Envoy request counter metric found")
            selectors = [envoy_counter_selector(name, instances) for name in names]
            # The canary phase may have finished between two scrapes. Wait until
            # every target's actual metric sample is newer than this barrier,
            # so its traffic cannot inflate the subsequent measured burst.
            baseline_barrier = time.time()
            deadline = time.monotonic() + (75 if self.args.quick else 120)
            baseline = None
            while time.monotonic() < deadline and baseline is None:
                for selector in selectors:
                    timestamp_query = f"min by (instance) (timestamp({selector}))"
                    try:
                        scraped = require_fresh_samples(api(base, "/api/v1/query", {"query": timestamp_query}), instances, baseline_barrier)
                        query = f"sum({selector})"
                        before = prom_value(api(base, "/api/v1/query", {"query": query}))
                        baseline = (query, timestamp_query, before, scraped)
                        break
                    except ValueError:
                        continue
                if baseline is None:
                    time.sleep(3)
            if baseline is None:
                raise ValueError("no complete fresh Envoy counter baseline after canary traffic")
            query, timestamp_query, before, baseline_scrapes = baseline
            count = 8 if self.args.quick else 30
            for _ in range(count):
                if self.request() != BODIES["v1"]:
                    raise ValueError("request failed while generating metric traffic")
            burst_barrier = time.time()
            deadline = time.monotonic() + (75 if self.args.quick else 120)
            while time.monotonic() < deadline:
                try:
                    after_scrapes = require_fresh_samples(api(base, "/api/v1/query", {"query": timestamp_query}), instances, burst_barrier)
                    after = prom_value(api(base, "/api/v1/query", {"query": query}))
                    require_counter_growth(before, after, count)
                except ValueError:
                    time.sleep(3)
                    continue
                return {"targets_up": len(envoy), "envoy_pods": len(pods), "query": query, "before": before, "after": after, "requests": count, "baseline_barrier": baseline_barrier, "baseline_scrapes": baseline_scrapes, "burst_barrier": burst_barrier, "after_scrapes": after_scrapes}
            raise ValueError("Envoy request counter did not increase after generated traffic and fresh scrapes")

    def logs(self):
        marker = str(uuid.uuid4())
        start = time.time_ns() - 5_000_000_000
        if self.request(path=f"/?verify={marker}") != BODIES["v1"]:
            raise ValueError("log proof request failed")
        self.request(path=f"/missing-{marker}", expected=404)
        service = self.service("loki", {"app.kubernetes.io/name": "loki"}, 3100)
        found = set()
        with port_forward(service, 3100) as base:
            deadline = time.monotonic() + (90 if self.args.quick else 180)
            while time.monotonic() < deadline:
                for stream in {"stdout", "stderr"} - found:
                    query = '{app="demo",namespace="demo",stream=' + json.dumps(stream) + '} |= ' + json.dumps(marker)
                    payload = api(base, "/loki/api/v1/query_range", {"query": query, "start": start, "end": time.time_ns(), "limit": 100, "direction": "backward"})
                    if loki_contains(payload, marker, stream):
                        found.add(stream)
                if len(found) == 2:
                    return {"request_id": marker, "streams": sorted(found), "stdout_uri": f"/?verify={marker}", "stdout_status": 200, "stderr_missing_file": f"/missing-{marker}"}
                time.sleep(3)
        raise ValueError(f"Loki missing UUID log evidence for {sorted({'stdout', 'stderr'} - found)}")

    def reports(self):
        failed = sum(r["status"] != "passed" for r in self.results)
        report = {"schema_version": 1, "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), "mode": "quick" if self.args.quick else "full", "integration": True, "status": "failed" if failed else "passed", "checks": self.results}
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.args.report.write_text(json.dumps(report, indent=2) + "\n")
        root = ET.Element("testsuite", name="cluster-acceptance", tests=str(len(self.results)), failures=str(failed))
        for result in self.results:
            case = ET.SubElement(root, "testcase", name=result["name"], time=str(result["seconds"]))
            if result["status"] != "passed":
                ET.SubElement(case, "failure", message=result["error"])
        self.args.junit.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(self.args.junit, encoding="unicode", xml_declaration=True)
        print(f"Reports: {self.args.report}, {self.args.junit}", flush=True)
        return 1 if failed else 0

    def run(self):
        if self.check("prerequisites", self.prerequisites):
            for name, callback in (("node-cni-dns-readiness", self.cluster_network), ("namespace-pods-and-workloads", self.pods), ("gateway-and-route-conditions", self.gateway), ("http-https-paths", self.routes), ("tls-rejects-wrong-identity-and-ca", self.tls_negative), ("canary-weighted-distribution", self.canary), ("prometheus-envoy-counter-growth", self.metrics), ("loki-request-stdout-and-stderr", self.logs)):
                self.check(name, callback)
        return self.reports()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="NodePort host/IP (default VM_IP or first node InternalIP)")
    parser.add_argument("--quick", action="store_true", help="400 canary requests and shorter metric/log deadlines; all checks remain enabled")
    parser.add_argument("--ca", type=Path, default=Path(".secrets/ca.crt"))
    parser.add_argument("--pod-cidr", default=DEFAULT_POD_CIDR, help="Expected IPv4/IPv6 network for every active non-host-network Pod")
    parser.add_argument("--report", type=Path, default=Path("artifacts/verification.json"))
    parser.add_argument("--junit", type=Path, default=Path("artifacts/verification.xml"))
    return Verification(parser.parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
