#!/usr/bin/env python3
"""Bounded HTTP smoke, not a capacity benchmark. Pass means zero request errors."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import http.client
import ipaddress
import json
import math
from pathlib import Path
import socket
import threading
import time
import uuid

BODY = b'Hello World! version=v1\n'
REQUEST_TIMEOUT = 10.0
BATCH_TIMEOUT = 300.0


def ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError('--host requires a numeric IPv4 address') from exc


def budget(maximum):
    def parse(value):
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError('budget must be an integer') from exc
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError(f'budget must be between 1 and {maximum}')
        return number
    return parse


def parse_args(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=ipv4)
    parser.add_argument('--requests', type=budget(10000), default=1000)
    parser.add_argument('--concurrency', type=budget(50), default=10)
    parser.add_argument('--report', type=Path, default=Path('artifacts/load-smoke.json'))
    return parser.parse_args(arguments)


def summarize(results, requested, wall_seconds):
    latencies = sorted(result['seconds'] * 1000 for result in results)
    successful = sum(result['ok'] for result in results)
    not_started = requested - len(results)
    errors = requested - successful
    percentiles = {name: round(latencies[math.ceil(len(latencies) * quantile) - 1], 3) if latencies else None
                   for name, quantile in (('p50', .50), ('p95', .95), ('p99', .99), ('max', 1))}
    return {'status': 'passed' if errors == 0 else 'failed', 'requested': requested,
            'attempted': len(results), 'successful': successful, 'errors': errors, 'not_started': not_started,
            'wall_seconds': round(wall_seconds, 3), 'wall_rps': round(len(results) / wall_seconds, 3) if wall_seconds else 0,
            'latency_ms': percentiles, 'latency_scope': 'all attempted requests, including failures; nearest-rank percentiles',
            'error_examples': [result['error'] for result in results if not result['ok']][:20]}


def request(host, marker, batch_deadline):
    start = time.monotonic()
    deadline = min(start + REQUEST_TIMEOUT, batch_deadline)
    connection = http.client.HTTPConnection(host, 30080, timeout=max(.001, deadline - start))
    timer = None
    try:
        connection.connect()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        connected_socket = connection.sock
        connected_socket.settimeout(remaining)

        def expire():
            # A socket inactivity timeout alone does not bound a slow/dripping
            # response. Shutdown also interrupts blocking header/body reads.
            try:
                connected_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        connection.request('GET', f'/?load={marker}', headers={'Host': 'demo.test', 'Connection': 'close'})
        response = connection.getresponse()
        body = response.read(len(BODY) + 1)
        if response.status != 200:
            raise ValueError(f'HTTP {response.status}, expected 200')
        if body != BODY:
            raise ValueError('response body differs from exact v1 greeting')
        if time.monotonic() > deadline:
            raise TimeoutError()
        return {'ok': True, 'seconds': time.monotonic() - start}
    except (OSError, http.client.HTTPException, ValueError) as exc:
        error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return {'ok': False, 'seconds': time.monotonic() - start, 'error': error}
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


def run(args):
    marker = str(uuid.uuid4())
    start = time.monotonic()
    concurrency = min(args.concurrency, args.requests)
    batch_limit = min(BATCH_TIMEOUT, math.ceil(args.requests / concurrency) * REQUEST_TIMEOUT)
    deadline = start + batch_limit
    lock = threading.Lock()
    next_request = 0

    def worker():
        nonlocal next_request
        results = []
        while time.monotonic() < deadline:
            with lock:
                if next_request >= args.requests:
                    break
                next_request += 1
            results.append(request(args.host, marker, deadline))
        return results

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(concurrency)]
        results = [result for future in futures for result in future.result()]
    report = summarize(results, args.requests, time.monotonic() - start)
    report.update(schema_version=1, kind='HTTP load smoke, not a capacity benchmark',
                  timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(), host=args.host, port=30080,
                  http_host='demo.test', request_id=marker, concurrency=concurrency,
                  request_timeout_seconds=REQUEST_TIMEOUT, batch_timeout_seconds=batch_limit,
                  pass_criterion='zero failed or unstarted requests; no latency or throughput SLO')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(f"{report['status'].upper()}: {report['successful']}/{args.requests} exact v1 responses; errors={report['errors']}; wall_rps={report['wall_rps']}; {args.report}")
    return 0 if report['errors'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(run(parse_args()))
