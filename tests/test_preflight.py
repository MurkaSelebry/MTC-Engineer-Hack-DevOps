import importlib.util
import json
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "preflight.py"
spec = importlib.util.spec_from_file_location("preflight", MODULE)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def facts(self):
        return dict(os_id="ubuntu", os_version="24.04", arch="x86_64", cpus=8,
                    memory_bytes=16 * 1024**3, disk_total=48 * 1024**3,
                    disk_free=30 * 1024**3, networks=["192.168.0.18/16"])

    def collect_fixture(self, extra_interfaces=(), extra_routes=()):
        physical = {"ifname": "ens3", "addr_info": [{"local": "192.168.0.18", "prefixlen": 16}]}
        interfaces = [physical, *extra_interfaces]
        default = {"dst": "default", "dev": "ens3", "gateway": "192.168.0.1"}

        def ip_output(command, **kwargs):
            if command == ["ip", "-j", "route", "show", "default"]:
                return json.dumps([default]).encode()
            if command == ["ip", "-j", "-4", "route", "show", "table", "all"]:
                return json.dumps([default, *extra_routes]).encode()
            if command == ["ip", "-j", "-4", "addr", "show", "dev", "ens3"]:
                return json.dumps([physical]).encode()
            if command == ["ip", "-j", "-4", "addr", "show"]:
                return json.dumps(interfaces).encode()
            raise AssertionError(f"unexpected network command: {command}")

        def host_file(path, **kwargs):
            return {"/etc/os-release": 'ID=ubuntu\nVERSION_ID="24.04"\n',
                    "/proc/meminfo": "MemTotal: 16777216 kB\n"}[str(path)]

        disk = SimpleNamespace(total=48 * 1024**3, free=30 * 1024**3)
        with mock.patch.object(preflight.Path, "read_text", host_file), \
                mock.patch.object(preflight.subprocess, "check_output", side_effect=ip_output), \
                mock.patch.object(preflight.shutil, "disk_usage", return_value=disk), \
                mock.patch.object(preflight.platform, "machine", return_value="x86_64"), \
                mock.patch.object(preflight.os, "cpu_count", return_value=8):
            return preflight.collect()

    def test_accepts_fifty_gb_vm_and_non_overlapping_network(self):
        self.assertEqual(preflight.validate(self.facts(), "10.244.0.0/16", "10.96.0.0/12"), [])
        boundary = self.facts() | dict(memory_bytes=14 * 1024**3, disk_total=45 * 1024**3, disk_free=20 * 1024**3)
        self.assertEqual(preflight.validate(boundary, "10.244.0.0/16", "10.96.0.0/12"), [])

    def test_rejects_resources_below_validated_profile(self):
        for overrides in (dict(cpus=4), dict(memory_bytes=7 * 1024**3), dict(disk_total=44 * 1024**3)):
            with self.subTest(overrides=overrides):
                self.assertTrue(preflight.validate(self.facts() | overrides, "10.244.0.0/16", "10.96.0.0/12"))

    def test_rejects_calico_default_overlapping_vm_subnet(self):
        errors = preflight.validate(self.facts(), "192.168.0.0/16", "10.96.0.0/12")
        self.assertTrue(any("overlap" in x for x in errors), errors)

    def test_rejects_pod_service_overlap(self):
        errors = preflight.validate(self.facts(), "10.96.0.0/16", "10.96.0.0/12")
        self.assertTrue(any("overlap" in x for x in errors), errors)

    def test_rejects_unvalidated_os_and_architecture(self):
        facts = self.facts() | dict(os_version="22.04", arch="aarch64")
        errors = preflight.validate(facts, "10.244.0.0/16", "10.96.0.0/12")
        self.assertTrue(any("Ubuntu 24.04" in x for x in errors), errors)
        self.assertTrue(any("amd64" in x for x in errors), errors)

    def test_rejects_insufficient_free_disk_before_mutation(self):
        facts = self.facts() | dict(disk_free=19 * 1024**3)
        errors = preflight.validate(facts, "10.244.0.0/16", "10.96.0.0/12")
        self.assertTrue(any("free disk" in x for x in errors), errors)

    def test_collect_ignores_only_known_calico_interfaces_on_repeat(self):
        names = ("cali123", "vxlan.calico", "tunl0", "kube-ipvs0")
        interfaces = [{"ifname": name, "addr_info": [{"local": "10.244.0.1", "prefixlen": 32}]} for name in names]
        routes = [{"dst": "10.244.0.0/24", "dev": name} for name in names]
        routes += [{"dst": "10.244.0.0/26", "type": "blackhole", "protocol": "bird"}]
        facts = self.collect_fixture(interfaces, routes)
        self.assertEqual(facts["networks"], ["192.168.0.18/16"])
        self.assertEqual(preflight.validate(facts, "10.244.0.0/16", "10.96.0.0/12"), [])

    def test_secondary_nic_vpn_and_container_bridges_are_not_hidden(self):
        for name in ("ens4", "wg0", "tun1", "docker0", "podman0", "cni0", "br-demo"):
            with self.subTest(interface=name):
                facts = self.collect_fixture([{"ifname": name, "addr_info": [{"local": "10.244.3.1", "prefixlen": 24}]}])
                self.assertIn("10.244.3.1/24", facts["networks"])
                errors = preflight.validate(facts, "10.244.0.0/16", "10.96.0.0/12")
                self.assertTrue(any("overlap" in error for error in errors), errors)

    def test_vpn_route_overlap_is_detected_even_with_non_overlapping_address(self):
        interfaces = [{"ifname": "wg0", "addr_info": [{"local": "172.16.0.1", "prefixlen": 24}]}]
        routes = [{"dst": "10.96.0.0/16", "dev": "wg0", "table": 123}]
        facts = self.collect_fixture(interfaces, routes)
        self.assertIn("10.96.0.0/16", facts["networks"])
        self.assertTrue(any("overlap" in error for error in preflight.validate(facts, "10.244.0.0/16", "10.96.0.0/12")))


if __name__ == "__main__":
    unittest.main()
