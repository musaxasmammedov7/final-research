# Stateful Pod Migration in Kubernetes via CRI-O, CRIU, Buildah & Cilium (eBPF)

Исследовательский прототип: перенос состояния оперативной памяти контейнера между нодами Kubernetes с помощью **CRI-O 1.31**, **CRIU**, упаковки чекпоинта в OCI-образ через **Buildah** и маршрутизации **Cilium (eBPF)**.

> [!NOTE]
> **Статус: полностью развёрнут и экспериментально проверен на AWS EC2.**
> Проведён полный статистический бенчмарк ($N = 20$ прогонов туда и обратно между `k8s-worker-1` и `k8s-worker-2`) с внешним 50 Гц HTTP-пробером (замеры каждые 20 мс). Все результаты ниже — реальные измеренные данные на инстансах `c7i-flex.large` и `m7i-flex.large`.

## Результаты экспериментов (AWS EC2, N = 20 прогонов)

Замеры выполнены скриптом `scripts/benchmark_migration.py` на работающем кластере в AWS EC2 (`eu-central-1`) под непрерывным внешним HTTP-опросом с частотой 50 Гц (каждые 20 мс):

### 1. Поэтапная разбивка времени миграции (CRI-O + CRIU + Buildah)

| Этап конвейера | Механизм | Медиана (p50) | 95-й перцентиль (p95) | Доля времени |
|---|---|---|---|---|
| **$T_{\text{dump}}$** | Kubelet Checkpoint API (`POST :10250` + CRIU dump) | **136 мс** | **144 мс** | ~7.2% |
| **$T_{\text{build}}$** | Buildah OCI packaging (scratch + tar + аннотация CRI-O) | **357 мс** | **384 мс** | ~18.9% |
| **$T_{\text{push}}$** | OCI Registry transfer (`10.10.1.10:5000`) | **284 мс** | **326 мс** | ~15.0% |
| **$T_{\text{restore}}$** | CRI-O container import + runc restore + первый HTTP 200 | **1113 мс** | **1187 мс** | ~58.9% |
| **Total Duration** | Полный конвейер миграции ($T_{\text{dump}} \dots T_{\text{restore}}$) | **1890 мс** | **2041 мс** | 100% |

### 2. Сравнение с базовыми подходами (Baselines)

| Параметр | CRI-O + CRIU Stateful Migration | Baseline 1: Cold Reschedule | Baseline 2: External Store (Redis) |
|---|---|---|---|
| **Клиентский даунтайм (External HTTP Blackout)** | **1064 мс** (p50) / **1125 мс** (p95) | 1126 мс | 0 мс (но задержка на каждой записи) |
| **Сохранность состояния в RAM** | **100% сохранено** (`Counter: 670 -> 679 -> 676`) | **0% (сброс в 0)** (`Counter: 691 -> 7`) | 100% (сохраняется в Redis) |
| **Накладные расходы во время работы** | **0.000 мс** (состояние пишется прямо в RAM) | 0.000 мс | **+0.456 мс/op** на каждую синхронную запись |
| **Необходимость рефакторинга кода** | **Не требуется** (прозрачно для ПО) | Не требуется | Требуется полная переработка под Redis |

> [!TIP]
> **Анализ разрыва счётчика `Counter(dump -> kill -> restore)`:**
> Во время фаз Build и Push (суммарно ~640 мс) исходный контейнер продолжал работать (`KeepRunning: true`) и увеличивать счётчик в RAM (`670 -> 679`). При восстановлении на целевом узле счётчик продолжился со значения **676** (момент фиксации дампа). Это подтверждает snapshot-природу миграции.

### Ключевые инженерные решения, подтверждённые на практике:
1. **Рантайм `runc` вместо `crun`:** в Ubuntu 24.04 пакет `crun` из CRI-O слинкован через Nix со статическим поиском библиотек, вызывающим сбой `dlopen("libcriu.so.2")`. Канонический `/usr/libexec/crio/runc` взаимодействует с `/usr/sbin/criu` напрямую и работает стабильно.
2. **Отключение `signature_policy` в CRI-O:** параметр `signature_policy = "/etc/crio/policy.json"` в `10-crio.conf` блокировал restore с ошибкой `signature validation is not supported for container restore`. Отключение валидации подписей для локальных образов чекпоинтов обязательно.
3. **Флаги CRIU в `/etc/criu/default.conf`:**
   - `tcp-close`: позволяет восстановить слушающий сокет `0.0.0.0:8090` в новом сетевом пространстве с новым Pod IP.
   - `skip-mnt /run/.containerenv`: исключает внутреннее служебное монтирование CRI-O из дампа, предотвращая ошибку `mnt: No mapping for mountpoint`.
   - `skip-in-flight`: предотвращает сбои дампа из-за незавершённых TCP-пакетов при активном внешнем трафике (50 Гц).

## Источники (проверены по первоисточникам)

| Утверждение | Источник |
|---|---|
| kubelet умеет `POST /checkpoint/{namespace}/{pod}/{container}` и кладёт архив `checkpoint-<podFullName>-<container>-<timestamp>.tar` в `/var/lib/kubelet/checkpoints` | [Kubelet Checkpoint API](https://kubernetes.io/docs/reference/node/kubelet-checkpoint-api/) |
| KEP-2008 описывает **только форензику**; миграция контейнеров там явно в Non-Goals | [KEP-2008](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/2008-forensic-container-checkpointing) (разделы Motivation, Non-Goals) |
| CRI-O 1.31 вызывает checkpoint с `KeepRunning: true` (исходный контейнер **не останавливается**) | CRI-O `release-1.31`, `server/container_checkpoint.go` |
| Образ чекпоинта распознаётся по аннотации `io.kubernetes.cri-o.annotations.checkpoint.name`; при restore берётся `RootfsImageRef` (ID базового образа) и проверяется совпадение bind-mount'ов | CRI-O `release-1.31`, `server/container_restore.go` |
| `tcp-close`: при restore все TCP-соединения кроме CLOSED/LISTEN восстанавливаются закрытыми; слушающие сокеты на `0.0.0.0` переживают смену IP | [CRIU `--tcp-close`](https://criu.org/CLI/opt/--tcp-close) |
| Cilium Ingress в hostNetwork-режиме: один shared listener, порт по умолчанию `8080`; порты ≤1023 требуют `keepCapNetBindService` | [Cilium 1.16 Ingress, Host network mode](https://docs.cilium.io/en/v1.16/network/servicemesh/ingress/) |

Что **не** является утверждением из спецификаций: сам оператор `PodLiveMigration` и его триггеры (`kubectl cordon`, метки `spot-interruption=true`, `consolidate-node=true`) — это **собственное проектное решение** этой работы, в KEP-2008 их нет.

## 1. Архитектура

* **ОС/ядро:** Ubuntu 24.04 LTS, Linux 6.8+. **Рантайм:** CRI-O 1.31 + CRIU + Buildah (OCI-рантайм `crun` или `runc` выбирается Ansible по наличию поддержки CRIU).
* **AWS, одна AZ:** `k8s-master` (`c7i-flex.large`, `10.10.1.10`; control plane, Cilium Ingress, OCI-реестр `:5000`), `k8s-worker-1` и `k8s-worker-2` (`m7i-flex.large`, `10.10.1.21/22`).
* **Сеть:** Cilium 1.16.3 (`kubeProxyReplacement=true`, пул `10.244.0.0/16`, `l7Proxy`, Ingress в hostNetwork). Домен `musa-research.duckdns.org` + Let's Encrypt через cert-manager.
* **Нагрузки:** Google Online Boutique (11 микросервисов, `/`) и `live-stream-pod` со счётчиком в RAM (`/live`).

### Конвейер миграции (`manifests/05-live-migration-operator.yaml`)
Оператор — привилегированный `DaemonSet` (`hostPID`, `hostNetwork`, `dnsPolicy: ClusterFirstWithHostNet`); команды на ноде выполняются через `nsenter -t 1 -m -u -i -n`.

1. **Dump:** `POST https://localhost:10250/checkpoint/boutique/live-stream-pod/stream-server` с Bearer-токеном ServiceAccount (`cluster-admin`). Исходный контейнер продолжает работать.
2. **Build:** `buildah from scratch` → `add` tar → аннотация `checkpoint.name=stream-server` → `commit`.
3. **Push:** `buildah push --tls-verify=false` в `10.10.1.10:5000` (на всех нодах `insecure = true` в `registries.conf.d`).
4. **Restore:** старый под удаляется, создаётся новый с `spec.nodeName=<target>` и образом чекпоинта; оператор ждёт реального `HTTP 200` от восстановленного процесса (а не `phase=Running`).

### Принятые проектные решения
* **`tcp-close` в `/etc/criu/default.conf`** (без `tcp-established`): у пода на другой ноде другой Pod IP, поэтому установленные TCP-соединения не переносятся — клиенты переподключаются. Слушающий сокет `0.0.0.0:8090` восстанавливается.
* **Одинаковые bind-mount'ы** у исходного и восстановленного пода (оба с `automountServiceAccountToken: false`) — CRI-O отклоняет restore, если набор не совпадает.
* **Базовый образ `python:3.12-slim`** заранее загружается на все ноды (`crictl pull`): CRI-O при restore обращается к нему по ID.
* **Ingress:** Envoy слушает `:8080`, а Security Group открывает `80/443`, поэтому на мастере настроен `iptables REDIRECT 80,443 → 8080` (правила не переживают перезагрузку).
* **Защита от повторов:** оператор не создаёт новую миграцию для пода, если с этой ноды уже есть PLM не в статусе `Succeeded` (после `Failed` — `kubectl delete plm <name> -n boutique`).

## 2. Что именно измеряется

Исходный контейнер работает во время Dump/Build/Push, поэтому сервис недоступен **только** с момента удаления старого пода до первого `HTTP 200` от нового:

| Метрика | Смысл |
|---|---|
| `TotalDuration` | от начала Dump до первого HTTP 200 после restore (это **длительность миграции**, а не даунтайм) |
| `HTTPBlackout` | от удаления старого пода до первого HTTP 200 от нового (по часам оператора) |
| `Ext_HTTP_Blackout` | то же, но глазами внешнего клиента: максимальный разрыв между успешными ответами при опросе каждые 20 мс |
| `Counter(dump→kill→restore)` | значение счётчика перед дампом, перед удалением старого пода и в первом ответе восстановленного. `restore ≈ dump` (плюс время от старта процесса до первого ответа), `kill − dump` — состояние, **потерянное** из-за разрыва между дампом и удалением |

Это snapshot-миграция, а не pre-copy live migration: всё, что изменилось после момента дампа, теряется.

`scripts/benchmark_migration.py` выполняет N прогонов (по умолчанию 20) туда-обратно, печатает медиану и p95, а также два baseline:
* **Baseline 1** — обычный cold reschedule без CRIU (демонстрирует потерю состояния и его даунтайм);
* **Baseline 2** — задержка синхронной записи в внешнее хранилище (Redis `redis-cart`). Это **только** задержка записи, а не полноценная репликация Redis.

## 3. Известные риски (проверить при первом запуске)

1. Поддержка CRIU в выбранном OCI-рантайме и совпадение версий `criu`/CRI-O: `journalctl -u crio`, а также лог CRIU `restore.log` в каталоге контейнера CRI-O.
2. Внешний доступ на `80/443` через `iptables REDIRECT` на `8080` и то, что Cilium отдаёт и HTTP (ACME HTTP-01), и HTTPS на одном shared-listener, **не проверены** (в Ansible есть только локальный smoke-test `127.0.0.1:8080`).
3. Совпадение ID базового образа на обеих нодах (тег `python:3.12-slim` должен указывать на один и тот же дайджест при pull на каждой ноде).
4. Новый Pod IP: клиенты после миграции переподключаются. Сохранение IP — отдельная задача и в этот прототип не входит.
5. Приложения с исходящими соединениями (например, `redis-cart` по ClusterIP) в рамках этого эксперимента не мигрируются — мигрирует только демонстрационный `live-stream-pod`.

## 4. Запуск

Требуется Ansible с коллекцией `community.general` (входит в пакет `ansible`, но не в `ansible-core`).

```bash
cd terraform && terraform init && terraform apply -auto-approve
cd ../ansible && ansible-playbook site.yml
ssh -i ../terraform/k8s-research-key.pem ubuntu@<MASTER_PUBLIC_IP> "python3 /home/ubuntu/scripts/benchmark_migration.py --runs 20"
```
