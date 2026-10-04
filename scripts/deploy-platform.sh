#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ "$EUID" -eq 0 ]] || { echo 'Platform installation is invoked through Ansible sudo.' >&2; exit 1; }
export KUBECONFIG=${KUBECONFIG:-/etc/kubernetes/admin.conf}
cache=/var/cache/mtc-hack
mkdir -p "$cache" artifacts .state
chmod 700 .state
version() { .venv/bin/python -c 'import sys,yaml; print(yaml.safe_load(open("versions.yaml"))[sys.argv[1]])' "$1"; }
verify_checksum() { printf '%s  %s\n' "$2" "$1" | sha256sum --check --status; }
download() {
  local url=$1 destination=$2 checksum=$3
  if [[ -f "$destination" ]] && verify_checksum "$destination" "$checksum"; then return; fi
  curl --fail --location --silent --show-error --connect-timeout 10 --max-time 180 --retry 3 "$url" -o "$destination.part"
  verify_checksum "$destination.part" "$checksum"
  mv "$destination.part" "$destination"
}
failure() {
  local result=$?
  echo "Platform deployment failed at line $1 (exit $result). Resources retained for diagnosis." >&2
  kubectl --request-timeout=10s get pods -A >&2 || true
  kubectl --request-timeout=10s get events -A --field-selector type=Warning --sort-by=.lastTimestamp >&2 || true
  exit "$result"
}
trap 'failure $LINENO' ERR

eg_version=$(version envoy_gateway_version)
monitor_version=$(version monitoring_chart_version)
loki_version=$(version loki_chart_version)
eg_chart="$cache/gateway-helm-${eg_version}.tgz"
if [[ ! -f "$eg_chart" ]] || ! verify_checksum "$eg_chart" "$(version envoy_gateway_chart_sha256)"; then
  timeout 240 helm pull oci://docker.io/envoyproxy/gateway-helm --version "$eg_version" --destination "$cache"
fi
verify_checksum "$eg_chart" "$(version envoy_gateway_chart_sha256)"
monitor_chart="$cache/kube-prometheus-stack-${monitor_version}.tgz"
download "https://github.com/prometheus-community/helm-charts/releases/download/kube-prometheus-stack-${monitor_version}/kube-prometheus-stack-${monitor_version}.tgz" "$monitor_chart" "$(version monitoring_chart_sha256)"
loki_chart="$cache/loki-${loki_version}.tgz"
download "https://github.com/grafana-community/helm-charts/releases/download/loki-${loki_version}/loki-${loki_version}.tgz" "$loki_chart" "$(version loki_chart_sha256)"

for namespace in demo envoy-gateway-system observability; do
  kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
done
timeout 900 helm upgrade --install eg "$eg_chart" -n envoy-gateway-system \
  -f helm/envoy-gateway.yaml --wait --timeout 10m
kubectl wait --for=condition=Established crd/gateways.gateway.networking.k8s.io crd/httproutes.gateway.networking.k8s.io crd/envoyproxies.gateway.envoyproxy.io --timeout=120s

kubectl apply -k kubernetes/app
kubectl apply -k kubernetes/security
bash scripts/create-tls.sh
kubectl apply -k kubernetes/gateway
kubectl -n demo rollout status deployment/demo-v1 --timeout=300s
kubectl -n demo rollout status deployment/demo-v2 --timeout=300s
kubectl -n demo wait --for=condition=Programmed gateway/demo --timeout=300s

umask 077
python3 scripts/grafana-password.py prepare
printf admin > .secrets/grafana-user
kubectl -n observability create secret generic grafana-admin \
  --from-file=admin-user=.secrets/grafana-user --from-file=admin-password=.secrets/grafana-password \
  --dry-run=client -o yaml | kubectl apply --server-side --field-manager=mtc-grafana -f -
[[ -z "${DEPLOY_USER:-}" ]] || chown -R "$DEPLOY_USER" .secrets

timeout 900 helm upgrade --install monitoring "$monitor_chart" -n observability \
  -f helm/kube-prometheus-stack.yaml \
  --set grafana.admin.existingSecret=grafana-admin \
  --set grafana.admin.userKey=admin-user --set grafana.admin.passwordKey=admin-password \
  --wait --timeout 10m
python3 scripts/grafana-password.py migrate
kubectl wait --for=condition=Established crd/podmonitors.monitoring.coreos.com crd/prometheusrules.monitoring.coreos.com --timeout=120s
timeout 900 helm upgrade --install loki "$loki_chart" -n observability -f helm/loki.yaml --wait --timeout 10m

bash scripts/build-fluentd.sh
kubectl apply -k kubernetes/logging
# A changed local image must roll pods even though no external registry is used.
fluentd_image_id=$(cat .state/fluentd-image-id)
[[ "$fluentd_image_id" =~ ^sha256:[a-f0-9]{64}$ ]]
kubectl -n observability patch daemonset fluentd --type=merge \
  -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"mtc-hack/fluentd-image-id\":\"$fluentd_image_id\"}}}}}"
kubectl apply -k kubernetes/monitoring
kubectl -n observability rollout status daemonset/fluentd --timeout=300s
kubectl -n demo wait --for=condition=Ready pod -l app.kubernetes.io/name=demo --timeout=300s
kubectl -n demo get gateway,httproute
echo 'Platform reconciliation complete; run make verify to prove HTTP, TLS, metrics and collected logs.'
