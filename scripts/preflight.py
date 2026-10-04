#!/usr/bin/env python3
"""Read-only checks before this dedicated single-node demo is provisioned."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

GIB = 1024**3


def validate(facts, pod_cidr, service_cidr):
    errors = []
    if (facts["os_id"], facts["os_version"]) != ("ubuntu", "24.04"):
        errors.append("This tested profile requires Ubuntu 24.04.")
    if facts["arch"] != "x86_64":
        errors.append("This image/tool lock requires amd64 (x86_64).")
    if facts["cpus"] < 8 or facts["memory_bytes"] < 14 * GIB:
        errors.append("Need at least 8 CPUs and 14 GiB usable RAM for the validated 16 GB VM profile.")
    if facts["disk_total"] < 45 * GIB:
        errors.append("Need at least 45 GiB usable root disk for the validated 50 GB volume profile.")
    if facts["disk_free"] < 20 * GIB:
        errors.append("Need at least 20 GiB free disk for the 14 GiB data budget plus images/build space.")
    try:
        pod = ipaddress.ip_network(pod_cidr)
        service = ipaddress.ip_network(service_cidr)
        if pod.version != 4 or service.version != 4:
            errors.append("This profile supports IPv4 Pod and Service CIDRs only.")
        if pod.overlaps(service):
            errors.append("Pod and Service CIDRs overlap.")
        for address in facts["networks"]:
            network = ipaddress.ip_network(address, strict=False)
            if network.version == 4 and (pod.overlaps(network) or service.overlaps(network)):
                errors.append(f"Cluster CIDRs overlap host network {network}.")
    except ValueError as exc:
        errors.append(f"Invalid CIDR: {exc}")
    return errors


def cni_interface(name):
    return name.startswith("cali") or name in {"vxlan.calico", "tunl0", "kube-ipvs0"}


def collect():
    os_release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip('"')
    memory = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemTotal:"))) * 1024
    disk = shutil.disk_usage("/")
    # Inspect all tables to retain VPN policy routes, and all interfaces to
    # catch secondary NIC/container bridge conflicts. Only this profile's
    # known Kubernetes interfaces are excluded on repeated deployments.
    routes = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show", "table", "all"], timeout=10))
    if not any(route.get("dst") in ("default", "0.0.0.0/0") for route in routes):
        raise RuntimeError("No default route; configure VM networking first.")
    addresses = json.loads(subprocess.check_output(["ip", "-j", "-4", "addr", "show"], timeout=10))
    networks = {f"{item['local']}/{item['prefixlen']}"
                for row in addresses if not cni_interface(row.get("ifname", ""))
                for item in row.get("addr_info", [])}
    for route in routes:
        device, destination = route.get("dev"), route.get("dst")
        # Calico blackhole routes have no device; this check concerns routes
        # through non-CNI devices. Default routes do not reserve a subnet.
        if device and not cni_interface(device) and destination not in (None, "default", "0.0.0.0/0"):
            networks.add(destination)
    networks = sorted(networks)
    return dict(os_id=os_release.get("ID"), os_version=os_release.get("VERSION_ID"),
                arch=platform.machine(), cpus=os.cpu_count(), memory_bytes=memory,
                disk_total=disk.total, disk_free=disk.free, networks=networks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod-cidr", default="10.244.0.0/16")
    parser.add_argument("--service-cidr", default="10.96.0.0/12")
    args = parser.parse_args()
    try:
        facts = collect()
        errors = validate(facts, args.pod_cidr, args.service_cidr)
        print(json.dumps(dict(facts=facts, errors=errors), indent=2))
        return 1 if errors else 0
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
