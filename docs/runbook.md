# Runbook

Команды выполняются на VM из корня репозитория обычным deploy-пользователем; `kubectl` использует `~/.kube/config` или заданный `KUBECONFIG`. Для host-команд нужен sudo. Не публикуйте `.secrets`, admin kubeconfig, SSH-ключи и сырые Secrets вместе с диагностикой.

## Первичная диагностика

```bash
make status
kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp
kubectl -n demo get gateway,httproute -o wide
kubectl -n demo describe gateway demo
kubectl -n demo describe httproute demo
kubectl -n observability get pvc
kubectl get pv
./scripts/verify.sh --quick
```

Сначала смотрите имя упавшей проверки в `artifacts/verification.json`, затем соответствующий компонент. Отчет без успешного статуса не является пройденной приемкой. `artifacts/deploy.log` содержит вывод Ansible; перед передачей третьим лицам проверьте диагностические файлы на чувствительные данные.

## Кластер и повторный deploy

```bash
sudo systemctl status containerd kubelet --no-pager
sudo journalctl -u kubelet -u containerd --since '-15 min' --no-pager
kubectl --request-timeout=10s get --raw=/readyz
kubectl -n kube-system get pods -o wide
kubectl -n calico-system get pods -o wide
```

При недоступности API проверьте private IP, маршруты, kubelet/containerd и static Pods. Не используйте `kubeadm reset` как универсальное исправление: он разрушает кластер. При прерванном `kubeadm init` сохраните состояние и определите последнюю завершенную фазу; наличие `admin.conf` само по себе не доказывает завершение init. Изменение CIDR, адреса API или версии существующего кластера — отдельная миграция, а не обычный повтор deploy.

Если стенд создавался ранней версией, проверьте `kubectl get pods -A -o wide`: non-hostNetwork Pod должны иметь адреса `10.244.0.0/16`. Старая пакетная сеть Podman могла выдать служебным Pod адреса `10.88.0.0/16`. Текущая установка предотвращает это до kubeadm, но удаление CNI-файла не меняет уже созданные sandboxes. Для такого старого стенда после установки Calico требуется плановое пересоздание затронутых служебных Pod, затем полная приемка; автоматизация не удаляет произвольные чужие Pod.

При ошибке pinned apt/chart/image версии проверьте точный URL, digest и доступность источника. Не подменяйте версии на `latest`: изменение должно попасть в исходники и пройти повторные испытания. Повторить согласование после устранения причины: `make deploy`, затем `make verify`.

## Pending, ImagePullBackOff, CrashLoopBackOff

```bash
kubectl -n demo describe pod POD_NAME
kubectl -n observability describe pod POD_NAME
kubectl -n observability logs POD_NAME --all-containers --tail=100
kubectl get nodes -o wide
kubectl describe node
```

`Pending`: смотрите requests, taints, PVC/nodeAffinity и свободное место. Local PV привязан к исходному узлу; перенос Pod на другой узел не переносит данные. `ImagePullBackOff`: проверьте Internet/DNS и закрепленный image reference. Для Fluentd используется `imagePullPolicy: Never`, поэтому нужен локальный импорт **на этот узел**:

```bash
sudo ./scripts/build-fluentd.sh
sudo ctr -n k8s.io images ls -q
kubectl -n observability rollout restart daemonset/fluentd
kubectl -n observability rollout status daemonset/fluentd --timeout=300s
```

`CrashLoopBackOff`: проверьте `logs --previous`, exit code, конфиг и права на volume. Root directories и UID/GID устанавливает storage role. Не исправляйте ошибки прав широким `chmod -R 777`.

## HTTP, TLS и маршруты

```bash
kubectl -n demo get deploy,svc,endpointslices
kubectl -n envoy-gateway-system get pods,svc -o wide
kubectl -n demo get gateway demo -o yaml
kubectl -n demo get httproute demo canary -o yaml
```

У Gateway ожидаются актуальные `Accepted=True`, `Programmed=True`; у маршрутов — `Accepted=True`, `ResolvedRefs=True`. Убедитесь, что NodePort 30080/30443 открыт клиенту и `curl` задает hostname через `--resolve`. Ответ 404 на запрос по IP без `Host: demo.test` не доказывает сбой приложения.

Для ошибки TLS проверьте CA и SAN публичного сертификата:

```bash
openssl verify -CAfile .secrets/ca.crt .secrets/tls.crt
openssl x509 -in .secrets/tls.crt -noout -dates -ext subjectAltName
```

Не отключайте validation через `-k`. Не удаляйте `.secrets` при повторном deploy: это заменит доверие клиентов. Deploy перевыпускает истекший leaf или leaf с остатком менее 30 дней, сохраняя ключ и CA; сертификат заменяется атомарно. Неполный комплект, неверный SAN, другая подпись или несовпадающие ключи требуют восстановления matching файлов. Сам CA не ротируется автоматически; перед его истечением нужна согласованная смена доверия. После обновления Secret снова выполните полный verify.

## Prometheus не видит Envoy

```bash
kubectl -n observability get podmonitor,prometheusrule
kubectl -n observability describe podmonitor envoy-proxy
kubectl -n envoy-gateway-system get pods --show-labels
kubectl -n observability port-forward --address 127.0.0.1 svc/monitoring-kube-prometheus-prometheus 9090:9090
```

В `/targets` проверьте targets с `job="envoy-proxy"`; порт — 19001, путь — `/stats/prometheus`. PodMonitor должен иметь `release=monitoring`, выбирать сгенерированные Envoy pods и metrics port. `up=1` недостаточно: запросы через Gateway должны увеличивать request counter. Скрипт приемки обнаруживает его имя через Prometheus API и проверяет рост.

Alert rules выполняются в Prometheus, внешних уведомлений нет. Для просмотра Grafana/Prometheus с рабочей машины используйте loopback port-forward на VM плюс SSH-туннель из README.

## Loki или Fluentd не доставляют лог

```bash
kubectl -n observability get pods -o wide
kubectl -n observability logs -l app.kubernetes.io/name=fluentd --tail=100
kubectl -n observability logs -l app.kubernetes.io/name=loki --tail=100
kubectl -n demo logs -l app.kubernetes.io/name=demo --tail=30 --prefix
sudo du -sh /var/lib/mtc-hack/fluentd /var/lib/mtc-hack/loki
```

Сначала создайте новый UUID-запрос и `/missing-UUID` через Gateway. Наличие UUID в `kubectl logs` подтверждает генерацию; доставка считается доказанной только после его появления в Loki отдельно в stdout и stderr. В Grafana Explore выберите корректный временной диапазон и `{app="demo",namespace="demo"}`.

Проверьте доступность symlink targets `/var/log/pods`, CRI parser, последовательность concat → JSON parser, labels и URL `http://loki.observability.svc.cluster.local:3100`. Ошибка о переполненном буфере требует восстановления Loki или освобождения диска. Не удаляйте `containers.pos`/buffer ради зеленого статуса: это может потерять очередь или вызвать повторное чтение.

## Диск, retention и сохранность

```bash
df -h /
sudo du -sh /var/lib/mtc-hack/* /var/lib/containerd /var/lib/containers /var/cache/mtc-hack
kubectl -n observability get pvc -o wide
```

Размеры PV не ограничивают расход host filesystem. Retention Loki не мгновенный и не является квотой; Prometheus WAL тоже занимает место сверх metric blocks. При нехватке места остановите нагрузку, устраните причину роста и подготовьте перенос/расширение хранилища. Удалять живые Loki chunks, TSDB, containerd directories или bound PV вручную нельзя.

`Retain` сохраняет PV после удаления claim, но новый PVC не обязательно автоматически привяжется к `Released` PV. Возврат claimRef и восстановление данных требуют контролируемой процедуры; обычный deploy не выполняет удаления/переиспользования данных.

После планового рестарта Pod/VM дождитесь readiness и повторите полный verify. Проверка сохраненного UUID после рестарта — отдельное доказательство persistence. Результаты восстановления, NetworkPolicy и нагрузки записываются в [validation.md](validation.md), без обещаний zero downtime для одного узла.

## Вход в Grafana

Пароль создается без завершающего перевода строки. При обновлении раннего стенда deploy согласует пароль в Grafana, локальном файле и Secret через `scripts/grafana-password.py`; прежний перевод строки мог быть частью пароля в базе. Миграция проверяет вход до и после изменения и безопасно повторяется после прерывания. Не меняйте только Secret: существующая база Grafana не переустанавливает пароль администратора из переменной окружения.

Helper выполняется с правами Ansible become, чтобы сохранить UID/GID и режим файла. Для отдельного повторения на выделенной VM: `sudo env KUBECONFIG=/etc/kubernetes/admin.conf python3 scripts/grafana-password.py migrate`. При смене учетных данных checksum-аннотация обновляет Pod и окружение sidecar; без изменений rollout не происходит. После ранее прерванной миграции возможно одно ожидание 310 секунд для истечения временной защиты входа Grafana. При отказе обоих вариантов авторизации helper останавливается, не сбрасывая вручную измененный пароль.

## Локальные проверки и упаковка

`make test` запускает Python tests, `make lint` — также shell/Python/YAML syntax, доступный shellcheck и Kustomize render. `bootstrap.sh` устанавливает `shellcheck` и `poppler-utils`; вне Ubuntu для тестов упаковки допустим `pypdf` в отдельном окружении. Пропуск инструмента явно выводится. CI устанавливает необходимые инструменты, но статус реального CI run нужно фиксировать отдельно.

Упаковка: `python3 scripts/package.py --repo-url "$PUBLIC_MAIN_URL" --name sel --passport docs/passport/Паспорт.pdf --output dist`. Нужна ссылка на настоящую опубликованную `main`. Упаковщик выполняет `git ls-remote` без credential helper и пользовательского Git config; закрытый репозиторий, отсутствие main или недоступная сеть останавливают сборку. После упаковки проверьте PDF визуально и загрузите `dist/sel.zip` на страницу задания самостоятельно.
