# Metrics and logs

The single-node stack runs kube-prometheus-stack 91.9.0, Loki chart 18.13.7
(application 3.7.8), and a custom Fluentd 1.19.4 image. The charts provide
Prometheus, Grafana, node-exporter and kube-state-metrics. Envoy is scraped via a
PodMonitor. Alert rules are evaluated in Prometheus; Alertmanager and external
notifications are intentionally absent.

Grafana provisions Prometheus (`uid: prometheus`), Loki (`uid: loki`), the standard
Kubernetes dashboards, and **MTC demo · traffic and logs** (`uid: mtc-demo`).
Grafana plugin preinstallation and automatic plugin updates are disabled to avoid
implicit, unpinned runtime downloads. The built-in data sources and panels suffice.
This dashboard covers available replicas, gateway request rates and response
classes, application CPU/memory, node disk space, stdout and stderr.

All observability services are ClusterIP. Use an SSH tunnel or a local kubeconfig
and port-forward; do not publish unauthenticated Loki or Prometheus:

```sh
kubectl -n observability port-forward svc/monitoring-grafana 3000:80
# In another terminal if needed:
kubectl -n observability port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090
kubectl -n observability port-forward svc/loki 3100:3100
```

The deployment script creates Grafana's admin password in the ignored
`.secrets/grafana-password` file and provisions the `grafana-admin` Secret.
Log in as `admin` using that local password. Do not put it in reports or source control.

Useful queries:

```promql
up{job="envoy-proxy"}
sum(rate(envoy_http_downstream_rq_total{job="envoy-proxy",envoy_http_conn_manager_prefix=~"http-10080|https-10443"}[5m]))
sum by (deployment) (kube_deployment_status_replicas_available{namespace="demo"})
```

The Envoy selector includes only the demo HTTP/HTTPS listeners. Admin, readiness
and Prometheus scrape requests are excluded from traffic rates and error ratios.

```logql
{app="demo",namespace="demo",stream="stdout"} | json
{app="demo",namespace="demo",stream="stdout"} | json | status >= 400
{app="demo",namespace="demo",stream="stderr"} |= "/missing-"
```

## Storage and resource choices

The repository provisions local PVs selected by `mtc-hack/component` with values
`prometheus`, `loki` and `grafana`, respectively. All claims use `mtc-local`.
Prometheus requests 5 GiB and retains at most 24 hours / 3 GB of metric blocks;
Loki requests 8 GiB and uses TSDB/v13, a filesystem store and the compactor for
24-hour retention. Grafana requests 1 GiB. Loki's minimum index period for this
retention configuration is 24 hours. Deletion is asynchronous and has an
additional one-hour delete delay.

Local PV capacities are scheduling metadata, not filesystem quotas. Loki does
not delete data automatically in response to low free disk space. The dashboard
and volume/node alerts help detect pressure. This profile assumes demonstration
traffic; sustained high-rate ingestion needs a larger disk or stricter admission
limits. Local storage and single replicas do not provide node failure tolerance.

Kubeadm etcd, controller-manager, scheduler and kube-proxy scrape targets are
omitted because their endpoints are not safely reachable with the default
kubeadm bindings. API server, kubelet/cAdvisor, node and workload metrics remain.
The Prometheus alert about a missing Alertmanager is disabled because no
notification service is configured.

## Fluentd data path

`kubernetes/logging/fluent.conf` is the single runtime configuration. The tail
input reads only `demo-*_demo_nginx-*.log` symlinks. Mounting `/var/log` also makes
their `/var/log/pods` targets accessible. The CRI parser first separates timestamp,
stream and message. `concat` reassembles partial records using a unique file tag
and stdout/stderr stream identity. A copy of the CRI stream avoids the concat
plugin's removal of its internal stream key. Timeout fragments are routed through
the normal output path. Only then does the JSON parser enrich access records;
plain error text remains in `message`.

Labels are deliberately limited to `app=demo`, `namespace=demo`, and `stream`.
URI, request ID and source path remain fields in the JSON line. The outer CRI
record timestamp is authoritative. The pipeline retains the original message
alongside parsed access fields to simplify debugging.

Positions and the 512 MiB file buffer live at `/var/lib/mtc-hack/fluentd` on the
node. The buffer blocks when full and retries indefinitely with bounded backoff.
This limits queue disk usage while allowing a temporary Loki outage. It is not
an exactly-once guarantee: retries can duplicate records, logs can rotate away
during a sufficiently long outage, and an abrupt process kill can lose an
unfinished fragment still held by concat in memory.

Fluentd runs as root only to read kubelet's log files and write the host-owned
state directory. It has no Kubernetes API token, no added capabilities, a
read-only root filesystem and read-only access to the host logs. Plugins are
installed at image-build time, never at Pod startup.

## Image and local pipeline verification

Build/import on each target Ubuntu amd64 node:

```sh
sudo ./scripts/build-fluentd.sh
kubectl apply -k kubernetes/logging
kubectl -n observability rollout status daemonset/fluentd --timeout=180s
```

The build pins the maintained Ruby 3.4.11 amd64 base by digest, Debian build dependencies
by archive snapshot, and all Ruby dependencies with Gemfile.lock checksums.
The script dry-runs the actual Fluentd image, imports it to the `k8s.io`
containerd namespace, and tags the normalized `docker.io/mtc-hack/fluentd:1.0.0`
name. The script writes its actual image identity to `.state/fluentd-image-id`;
the deployment script uses that content identity as a Pod template annotation
to roll out changed images without restarting unchanged ones. Kustomize hashes
the Fluentd ConfigMap name to trigger a rollout when its configuration changes.
A host flock serializes concurrent builds/imports. Build-time host networking
avoids interference between the Podman bridge and Kubernetes networking; the
configuration dry-run has no network access. `imagePullPolicy: Never` ensures deployment uses that local build.

For development, install Ruby 3.4 and Bundler 2.6.8. A real-process test runs the
production config against generated CRI logs and a temporary HTTP receiver,
asserting the actual Loki push payload, JSON access fields, text stderr,
interleaved stream separation, incomplete-fragment timeout delivery, labels and
nonempty positions:

```sh
BUNDLE_GEMFILE=images/fluentd/Gemfile bundle install
BUNDLE_GEMFILE=images/fluentd/Gemfile bundle exec python3 images/fluentd/test_pipeline.py
```

This process test does not replace `./scripts/verify.sh` on the running cluster:
that acceptance command must prove externally generated requests appear in
Prometheus and both Loki streams.

## Upstream references

- [Ruby 3.4.11 release](https://www.ruby-lang.org/en/news/2026/09/23/ruby-3-4-11-released/)
- [Monolithic Loki](https://grafana.com/docs/loki/latest/setup/install/helm/install-monolithic/)
- [Loki retention and filesystem caveats](https://grafana.com/docs/loki/latest/operations/storage/retention/)
- [Fluentd Loki client labels and output format](https://grafana.com/docs/loki/latest/send-data/fluentd/)
- [CRI parser](https://github.com/fluent/fluent-plugin-parser-cri)
- [Concat stream identity and timeout handling](https://github.com/fluent-plugins-nursery/fluent-plugin-concat)
- [Fluentd tail rotation and position files](https://docs.fluentd.org/input/tail)
- [Prometheus Operator monitor troubleshooting](https://prometheus-operator.dev/docs/platform/troubleshooting/)
