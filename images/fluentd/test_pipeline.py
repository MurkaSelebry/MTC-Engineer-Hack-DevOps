#!/usr/bin/env python3
"""Exercise the real CRI->concat->JSON->Loki pipeline, including interleaving.

Run under `bundle exec python3` with the locked Fluentd gems installed.
No Kubernetes cluster or live Loki is required.
The local HTTP sink validates the plugin's actual Loki push payload.
"""
import gzip
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "kubernetes/logging/fluent.conf"


def main():
    assert CONFIG.is_file(), "Production Fluentd config is missing"
    command = os.environ.get("FLUENTD_BIN", "fluentd")
    assert shutil.which(command), f"Fluentd executable missing: {command}"
    records = []

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/loki/api/v1/push", self.path
            data = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            records.extend(json.loads(data)["streams"])
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="fluentd-test-") as tmp:
        folder = Path(tmp)
        log_a = folder / "demo-v1-a_demo_nginx-a.log"
        log_b = folder / "demo-v2-b_demo_nginx-b.log"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        log_a.write_text(f'{stamp}.001000000Z stdout P {{"uri":"/part-\n'
                         f'{stamp}.002000000Z stderr F plain stderr request=/missing-proof\n'
                         f'{stamp}.003000000Z stdout F proof","status":200,"time":"{stamp}+00:00"}}\n')
        log_b.write_text(f'{stamp}.002000000Z stdout F {{"uri":"/other-pod","status":201}}\n'
                         f'{stamp}.004000000Z stderr P timeout-preserved\n')
        env = dict(os.environ, FLUENT_LOG_PATH=str(folder / "*.log"),
                   FLUENT_STATE_PATH=str(folder / "state"),
                   LOKI_URL=f"http://127.0.0.1:{server.server_port}")
        (folder / "state").mkdir()
        with (folder / "fluentd-output.txt").open("w+") as output:
            process = subprocess.Popen([command, "--no-supervisor", "-c", str(CONFIG)],
                                       env=env, stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 25
                while sum(len(s.get("values", [])) for s in records) < 4 and time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.2)
            finally:
                process.terminate()
                process.wait(timeout=10)
            output.seek(0)
            diagnostics = output.read()
        lines = [(stream["stream"], json.loads(value[1]))
                 for stream in records for value in stream["values"]]
        assert len(lines) == 4, f"Expected 4 events, got {lines}\n{diagnostics}"
        access = next(record for labels, record in lines if record.get("uri") == "/part-proof")
        assert access["status"] == 200, access
        assert access["time"] == f"{stamp}+00:00", access
        assert all(labels["app"] == "demo" and labels["namespace"] == "demo" for labels, _ in lines)
        errors = [record for labels, record in lines if labels["stream"] == "stderr"]
        assert sorted(record["message"] for record in errors) == ["plain stderr request=/missing-proof", "timeout-preserved"], errors
        assert any(record.get("uri") == "/other-pod" for _, record in lines), lines
        assert (folder / "state/containers.pos").stat().st_size > 0
    server.shutdown()
    print("PASS: actual CRI parsing, partial assembly, stream/pod separation, JSON, stderr, timeout rescue, Loki labels and positions")


if __name__ == "__main__":
    main()
