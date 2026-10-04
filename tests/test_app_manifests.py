"""Semantic acceptance checks for the rendered demo application and Gateway."""

from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
KUSTOMIZATIONS = (
    ROOT / "kubernetes" / "app",
    ROOT / "kubernetes" / "gateway",
    ROOT / "kubernetes" / "security",
)
NGINX_IMAGE = (
    "docker.io/nginxinc/nginx-unprivileged:1.30.5-alpine3.24"
    "@sha256:f4522a5f23f9caebcae9be6e3940ea0599a2b9eb05222c7118d01fc887e848d6"
)


def render(path: pathlib.Path) -> list[dict]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(path)],
        check=True,
        text=True,
        capture_output=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


class ManifestAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = [document for path in KUSTOMIZATIONS for document in render(path)]
        cls.resources = {
            (
                document["kind"],
                document["metadata"]["name"],
                document["metadata"].get("namespace", ""),
            ): document
            for document in cls.documents
        }
        if len(cls.resources) != len(cls.documents):
            raise AssertionError("rendered resource identities must be unique")

    @classmethod
    def resource(cls, kind: str, name: str, namespace: str = "demo") -> dict:
        return cls.resources[(kind, name, namespace)]

    @classmethod
    def resource_with_name_prefix(cls, kind: str, prefix: str, namespace: str = "demo") -> dict:
        matches = [
            document
            for (resource_kind, name, resource_namespace), document in cls.resources.items()
            if resource_kind == kind and resource_namespace == namespace and name.startswith(prefix)
        ]
        if len(matches) != 1:
            raise AssertionError(f"expected one {kind} with prefix {prefix}, got {len(matches)}")
        return matches[0]

    def test_application_services_select_ready_hardened_backends(self):
        for version, replicas in (("v1", 2), ("v2", 1)):
            deployment = self.resource("Deployment", f"demo-{version}")
            service = self.resource("Service", f"demo-{version}")
            spec = deployment["spec"]
            pod_spec = spec["template"]["spec"]
            pod_labels = spec["template"]["metadata"]["labels"]
            container = pod_spec["containers"][0]

            self.assertEqual(spec["replicas"], replicas)
            self.assertEqual(spec["selector"]["matchLabels"], pod_labels)
            self.assertEqual(pod_labels["app.kubernetes.io/name"], "demo")
            self.assertEqual(pod_labels["app.kubernetes.io/version"], version)
            self.assertEqual(service["spec"]["type"], "ClusterIP")
            self.assertEqual(service["spec"]["selector"], pod_labels)
            self.assertEqual(service["spec"]["ports"], [{"port": 80, "targetPort": "http"}])

            self.assertEqual(container["image"], NGINX_IMAGE)
            self.assertEqual(container["ports"], [{"name": "http", "containerPort": 8080}])
            self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/healthz")
            self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/healthz")
            self.assertEqual(container["startupProbe"]["httpGet"]["path"], "/healthz")
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
            self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
            self.assertEqual(pod_spec["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
            for bound in ("requests", "limits"):
                self.assertIn("cpu", container["resources"][bound])
                self.assertIn("memory", container["resources"][bound])

    def test_nginx_contract_exposes_health_and_logs_real_missing_files(self):
        for version in ("v1", "v2"):
            prefix = f"demo-{version}-nginx-"
            config = self.resource_with_name_prefix("ConfigMap", prefix)
            config_name = config["metadata"]["name"]
            self.assertRegex(config_name, rf"^{re.escape(prefix)}[a-z0-9]{{10}}$")
            deployment = self.resource("Deployment", f"demo-{version}")
            volumes = {volume["name"]: volume for volume in deployment["spec"]["template"]["spec"]["volumes"]}
            self.assertEqual(volumes["config"]["configMap"]["name"], config_name)
            nginx = config["data"]["nginx.conf"]
            self.assertIn("listen 8080", nginx)
            self.assertIn("access_log /dev/stdout json", nginx)
            self.assertIn("error_log /dev/stderr", nginx)
            self.assertIn("log_not_found on", nginx)
            self.assertIn("location = /healthz", nginx)
            self.assertIn('"request_id":"$request_id"', nginx)
            self.assertIn('"correlation_id":"$correlation_id"', nginx)
            self.assertIn('"~^[A-Za-z0-9._:-]{1,128}$" $http_x_request_id', nginx)
            self.assertIn('"uri":"$request_uri"', nginx)
            self.assertEqual(config["data"]["index.txt"], f"Hello World! version={version}\n")

    def test_gateway_exposes_fixed_http_and_https_nodeports(self):
        proxy = self.resource("EnvoyProxy", "demo-proxy")
        service = proxy["spec"]["provider"]["kubernetes"]["envoyService"]
        self.assertEqual(service["type"], "NodePort")
        self.assertEqual(service["externalTrafficPolicy"], "Cluster")
        patch = service["patch"]
        self.assertEqual(patch["type"], "StrategicMerge")
        self.assertEqual(
            patch["value"]["spec"]["ports"],
            [{"port": 80, "nodePort": 30080}, {"port": 443, "nodePort": 30443}],
        )

        gateway_class = self.resource("GatewayClass", "eg", "")
        self.assertEqual(gateway_class["spec"]["parametersRef"]["name"], "demo-proxy")
        self.assertEqual(gateway_class["spec"]["parametersRef"]["namespace"], "demo")
        gateway = self.resource("Gateway", "demo")
        listeners = {listener["name"]: listener for listener in gateway["spec"]["listeners"]}
        self.assertEqual((listeners["http"]["protocol"], listeners["http"]["port"]), ("HTTP", 80))
        self.assertEqual((listeners["https"]["protocol"], listeners["https"]["port"]), ("HTTPS", 443))
        self.assertEqual(
            listeners["https"]["tls"]["certificateRefs"],
            [{"kind": "Secret", "name": "demo-tls"}],
        )

    def test_routes_preserve_default_rewrite_v2_and_weighted_canary_behavior(self):
        services = {name for kind, name, namespace in self.resources if kind == "Service" and namespace == "demo"}
        route = self.resource("HTTPRoute", "demo")
        self.assertEqual(route["spec"]["hostnames"], ["demo.test"])
        v2_rule, default_rule = route["spec"]["rules"]
        self.assertEqual(v2_rule["matches"][0]["path"], {"type": "PathPrefix", "value": "/v2"})
        self.assertEqual(
            v2_rule["filters"],
            [{"type": "URLRewrite", "urlRewrite": {"path": {"type": "ReplaceFullPath", "replaceFullPath": "/"}}}],
        )
        self.assertEqual(v2_rule["backendRefs"], [{"name": "demo-v2", "port": 80}])
        self.assertEqual(default_rule["backendRefs"], [{"name": "demo-v1", "port": 80}])

        canary = self.resource("HTTPRoute", "canary")
        self.assertEqual(canary["spec"]["hostnames"], ["canary.test"])
        backends = canary["spec"]["rules"][0]["backendRefs"]
        self.assertEqual({backend["name"]: backend["weight"] for backend in backends}, {"demo-v1": 90, "demo-v2": 10})
        self.assertEqual(sum(backend["weight"] for backend in backends), 100)
        self.assertTrue({backend["name"] for backend in backends}.issubset(services))

    def test_network_policy_allows_only_the_actual_envoy_gateway_pods(self):
        policy = self.resource("NetworkPolicy", "demo-ingress")
        self.assertEqual(policy["spec"]["podSelector"]["matchLabels"], {"app.kubernetes.io/name": "demo"})
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(policy["spec"]["egress"], [])
        ingress = policy["spec"]["ingress"]
        self.assertEqual(ingress[0]["ports"], [{"protocol": "TCP", "port": 8080}])
        peer = ingress[0]["from"][0]
        self.assertEqual(
            peer["namespaceSelector"]["matchLabels"],
            {"kubernetes.io/metadata.name": "envoy-gateway-system"},
        )
        self.assertEqual(
            peer["podSelector"]["matchLabels"],
            {
                "app.kubernetes.io/component": "proxy",
                "app.kubernetes.io/managed-by": "envoy-gateway",
                "app.kubernetes.io/name": "envoy",
                "gateway.envoyproxy.io/owning-gateway-name": "demo",
                "gateway.envoyproxy.io/owning-gateway-namespace": "demo",
            },
        )


if __name__ == "__main__":
    unittest.main()
