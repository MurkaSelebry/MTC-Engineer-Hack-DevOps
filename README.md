# MTC Engineer Hack: Kubernetes, Gateway API и observability

Демонстрационный Nginx публикуется через Envoy Gateway. Prometheus собирает метрики Envoy и Kubernetes, Fluentd передает access/error-логи в Loki, Grafana объединяет метрики и поиск логов. Кластер создается `kubeadm` на выделенной Ubuntu 24.04 amd64; Ansible, Helm и Kustomize выполняют развертывание из репозитория.

**Испытано на двух Ubuntu 24.04.5 LTS amd64**, включая VM с диском 50 GB: HTTP/HTTPS, метрики и логи, повторное развертывание, перезагрузка и сохранность данных. Команды, исходные JSON-отчеты и оставшиеся замечания независимой проверки: [docs/validation.md](docs/validation.md). [GitHub Actions: static PASS](https://github.com/MurkaSelebry/MTC-Engineer-Hack-DevOps/actions/runs/37229326629); integration при push не запускается.

## Среда и версии

Профиль: одна VM, **8 vCPU, 16 GB RAM, диск от 50 GB**, доступ в Интернет, стабильный IPv4, пользователь с passwordless sudo. Это выделенная тестовая машина: установка меняет containerd, swap, sysctl и настройки Kubernetes. Preflight требует не менее 20 GiB свободного места под образы, локальную сборку и данные; 50 GB подходит для ограниченного демонстрационного трафика. `make preflight` проверяет ОС, архитектуру, ресурсы, сеть и доступность источников.

| Компонент | Версия / источник фиксации |
| --- | --- |
| Ubuntu | 24.04 amd64; факт испытания — в протоколе приемки |
| Kubernetes / kubeadm | 1.35.9, пакет 1.35.9-1.1 |
| containerd | пакет 2.2.1-0ubuntu1~24.04.3 |
| Calico | 3.32.2, VXLAN |
| Envoy Gateway / Gateway API | v1.9.2 / 1.6.1 |
| Helm | 3.19.0 |
| kube-prometheus-stack | chart 91.9.0: Prometheus, Grafana, node-exporter, kube-state-metrics |
| Loki | chart 18.13.7, приложение 3.7.8 |
| Nginx | nginx-unprivileged 1.30.5-alpine3.24, образ закреплен digest |
| Fluentd | 1.19.4; Loki plugin 1.3.0, CRI parser 0.1.1, concat 2.6.2 |
| Podman / Ansible | пакет 4.9.3+ds1-1ubuntu0.2 / ansible-core 2.19.13 |

Версии и SHA256 архивов: [versions.yaml](versions.yaml); Python-зависимости: [requirements.lock](requirements.lock); Ruby-зависимости сборщика: [Gemfile.lock](images/fluentd/Gemfile.lock). Образы приложения и Fluentd base закреплены digest в исходных манифестах/Dockerfile.

## Развертывание

На новой VM клонируйте опубликованную ветку `main`. `REPO_CLONE_URL` — реальный HTTPS-адрес клонирования, без `/tree/main`:

```bash
sudo apt-get update && sudo apt-get install -y git
read -r -p 'HTTPS URL репозитория для clone: ' REPO_CLONE_URL
git clone --branch main "$REPO_CLONE_URL" mtc-hack
cd mtc-hack
./scripts/bootstrap.sh
make preflight
make deploy
make verify
```

`bootstrap` создает `.venv`; `deploy` устанавливает containerd, Kubernetes, Calico, локальные PV, Helm-компоненты, приложение, маршруты и NetworkPolicy. Образ `docker.io/mtc-hack/fluentd:1.0.0` **собирается на самой VM через Podman**, проверяется и импортируется в containerd: личный registry не нужен. Плагины не скачиваются при запуске Pod.

Kubeconfig копируется пользователю в `~/.kube/config`. Публичный сертификат локального CA находится в `.secrets/ca.crt`, закрытые ключи и пароль Grafana остаются в `.secrets/`. Для обычной проверки не нужен `sudo`. Если выбран другой kubeconfig, задайте `KUBECONFIG` в окружении.

Повторный `make deploy` согласует ресурсы; он не выполняет `kubeadm reset` и не удаляет PV/CA. Изменение параметров существующего кластера требует отдельной миграции. После повторного запуска снова выполните `make verify`.

Для проверки NodePort по нужному IP:

```bash
export VM_IP=192.0.2.10   # замените адресом своей VM
make verify VM_IP="$VM_IP"
# Быстрее, но все категории проверок включены:
./scripts/verify.sh --host "$VM_IP" --quick
```

Разрешите TCP 30080/30443 от проверяющего клиента; SSH — только от администратора. Prometheus, Loki и Grafana доступны через административный SSH tunnel. Установка защищает TCP 6443/10250/2379/2380 отдельной firewall-цепочкой: разрешены loopback, адрес узла и Pod CIDR; правило восстанавливается при deploy и загрузке ОС. Для удаленного kubectl используйте SSH. Публичный IP и private IP могут различаться. Команда с VM проверяет доступ с VM; внешнюю доступность подтвердите отдельным `curl` с другой машины.

## Приложение и Gateway API

`GatewayClass/eg` использует `EnvoyProxy/demo-proxy`. `Gateway/demo` и `HTTPRoute/demo`, `HTTPRoute/canary` находятся в namespace `demo`. Envoy data plane работает в `envoy-gateway-system`; фиксированные NodePort — HTTP **30080**, HTTPS **30443**, `externalTrafficPolicy: Cluster`.

| Host и путь | Ответ / поведение |
| --- | --- |
| `demo.test/` | `Hello World! version=v1` и перевод строки |
| `demo.test/v2`, `/v2/` | rewrite на `/`, ответ `Hello World! version=v2` |
| `canary.test/` | веса backend v1/v2 = 90/10 |
| `demo.test/missing-UUID` | 404, access-запись и сообщение об отсутствующем файле в stderr |

Services `demo-v1`, `demo-v2` передают порт 80 на контейнерный 8080. Реплики v1/v2: 2/1. TLS Secret `demo-tls` содержит сертификат для обоих hostname, подписанный локальным CA. Реальная DNS-запись для демонстрации не требуется:

```bash
curl --fail --resolve "demo.test:30080:$VM_IP" http://demo.test:30080/
curl --fail --resolve "demo.test:30080:$VM_IP" http://demo.test:30080/v2
curl --fail --cacert .secrets/ca.crt \
  --resolve "demo.test:30443:$VM_IP" https://demo.test:30443/
```

При запуске с другой машины скопируйте **только публичный** `.secrets/ca.crt` по SSH. Использовать `curl -k` для доказательства TLS нельзя.

## Проверка метрик и логов

`make verify` проверяет readiness, актуальные условия Gateway/HTTPRoute, точные HTTP/HTTPS-ответы, отрицательные TLS-сценарии, 1000 запросов canary, реальный рост счетчика Envoy и доставку UUID в оба потока Loki. Quick-режим использует 400 canary-запросов. Prometheus/Loki временно доступны проверяющему через автоматический loopback port-forward, который завершается после проверки.

Результат — `artifacts/verification.json` и `artifacts/verification.xml` (JUnit), ненулевой exit при любой обязательной ошибке. Для раздельных прогонов:

```bash
./scripts/verify.sh --host "$VM_IP" \
  --report artifacts/verification-vm-a.json \
  --junit artifacts/verification-vm-a.xml
```

Для ручного просмотра выполните на VM в отдельных терминалах:

```bash
kubectl -n observability port-forward --address 127.0.0.1 svc/monitoring-grafana 3000:80
kubectl -n observability port-forward --address 127.0.0.1 svc/monitoring-kube-prometheus-prometheus 9090:9090
kubectl -n observability port-forward --address 127.0.0.1 svc/loki 3100:3100
```

На рабочей машине установите SSH-туннель:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 3100:127.0.0.1:3100 user@VM_IP
```

Откройте Grafana `http://127.0.0.1:3000`, пользователь `admin`; пароль прочитайте **на своей VM** из `.secrets/grafana-password`. Он также хранится в Secret `observability/grafana-admin`. Не включайте пароль в скриншоты и отчеты. Dashboard: **MTC demo · traffic and logs**. Prometheus UI — `http://127.0.0.1:9090`.

PromQL для targets и запросов:

```promql
up{job="envoy-proxy"}
sum(rate(envoy_http_downstream_rq_total{job="envoy-proxy",envoy_http_conn_manager_prefix=~"http-10080|https-10443"}[5m]))
kube_deployment_status_replicas_available{namespace="demo"}
```

Метрики Envoy собираются PodMonitor с порта 19001, путь `/stats/prometheus`; проверка обнаруживает фактическое имя request counter. Дополнительно собираются API server, kubelet/cAdvisor, node-exporter и kube-state-metrics. Правила доступны в Prometheus; Alertmanager и внешняя доставка уведомлений не настроены.

Сгенерируйте маркер и 404:

```bash
MARKER=$(python3 -c 'import uuid; print(uuid.uuid4())')
curl --resolve "demo.test:30080:$VM_IP" "http://demo.test:30080/?verify=$MARKER"
curl --resolve "demo.test:30080:$VM_IP" "http://demo.test:30080/missing-$MARKER"
printf '%s\n' "$MARKER"
```

В Grafana Explore выберите Loki и подставьте маркер в оба запроса:

```logql
{app="demo",namespace="demo",stream="stdout"} |= "UUID"
{app="demo",namespace="demo",stream="stderr"} |= "UUID"
```

Fluentd читает CRI-логи только контейнеров приложения, соединяет фрагменты и передает JSON access-записи и текстовые error-записи. UUID/URI остаются полями строки, а не Loki labels. Подробнее: [observability](observability/README.md).

## Ограничения и материалы

- Один узел и локальные PV **не обеспечивают HA**. Реплики защищают от сбоя отдельного Pod, но не от потери VM.
- PV: Prometheus 5 GiB, Loki 8 GiB, Grafana 1 GiB, `mtc-local`, `Retain`, привязка к узлу. Fluentd имеет persistent hostPath и буфер 512 MiB. Емкость PV не является квотой файловой системы.
- Prometheus: retention 24h / 3 GB блоков; Loki: 24h с асинхронным удалением. Retention не защищает от заполнения диска при интенсивном потоке. Требуются контроль свободного места и достаточный запас.
- Локальный CA нужно явно доверить клиенту. При deploy leaf-сертификат с остатком менее 30 дней перевыпускается с тем же ключом и CA; постоянного планировщика продления нет. Ротация самого CA выполняется явно.
- NetworkPolicy ограничивает вход к приложению Envoy-подами и запрещает egress приложения. Проверка YAML не заменяет проверку enforcement; результаты сетевых и аварийных испытаний относятся к протоколу приемки.
- CI выполняет статические проверки и unit tests. Опциональный ручной integration job требует отдельного заранее настроенного self-hosted runner; такой runner не входит в установку. Наличие workflow не является доказательством выполненного CI.

[Архитектура](docs/architecture.md) · [Runbook](docs/runbook.md) · [Протокол испытаний](docs/validation.md) · [Обоснование и источники](docs/research.md)

Дополнительное испытание на выделенном демонстрационном стенде временно останавливает Loki, перезапускает Fluentd и удаляет один Pod приложения. Оно проверяет NetworkPolicy, восстановление Pod и доставку 100 access/error пар из очереди после сбоя:

```bash
python3 scripts/recovery-test.py --allow-disruption --host "$VM_IP"
```

Loki восстанавливается в `finally`; результат сохраняется в `artifacts/recovery.json`. Запускайте этот сценарий отдельно от deploy и обычной приемки.

Короткая нагрузочная проверка с 10 одновременными запросами и 1000 запросов суммарно:

```bash
python3 scripts/load-smoke.py --host "$VM_IP" --requests 1000 --concurrency 10
```

Она проверяет точный ответ каждого запроса, сохраняет ошибки, latency percentiles и throughput в `artifacts/load-smoke.json`. Это smoke-тест, не оценка предельной производительности.

## Подготовка сдачи

Готовый трехстраничный [паспорт PDF](docs/passport/Паспорт.pdf); [инструкция его пересборки](docs/passport/README.md).

Опубликуйте ветку `main` в публичном репозитории. Перед упаковкой проверьте анонимное клонирование в чистый каталог. Упаковщик не создает remote и ничего не отправляет в Git:

```bash
sudo apt-get install -y poppler-utils  # только для проверки PDF/упаковки
PUBLIC_MAIN_URL='https://github.com/MurkaSelebry/MTC-Engineer-Hack-DevOps/tree/main'
python3 scripts/package.py --repo-url "$PUBLIC_MAIN_URL" --name Резван \
  --passport docs/passport/Паспорт.pdf --output dist
```

Получится `dist/Резван.zip`, содержащий **только** `Ссылка.txt` с URL ветки и `Паспорт.pdf`. Упаковщик проверяет анонимную доступность `main`, PDF до 4 страниц и 15 MB, ZIP до 18 MB. Вместо `pdfinfo` допускается `pypdf` в отдельном окружении упаковки. Исходники и секреты в конкурсный ZIP не добавляются.

По условию архив загружается на страницу задания до **4 октября 23:59**, а изменения в `main` после этого срока запрещены. Репозиторий должен оставаться публичным до публикации списка финалистов. Имя конкурсного архива — `Резван.zip`, по фамилии участника при регистрации. После упаковки загрузите его на страницу задания кнопкой «Загрузить»; для замены используйте «Загрузить другой файл».

Повторная независимая проверка выявила оставшиеся ограничения: старый ConfigMap подменяет dashboard на VM A, служебные host ports доступны извне, на VM B есть лишний стартовый рестарт Operator. Установка на чистую ОС независимо не проверялась. Подробности и границы исходных отчетов — в [протоколе испытаний](docs/validation.md#повторная-независимая-проверка).
