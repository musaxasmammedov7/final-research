# Stateful Pod Migration in Kubernetes via CRI-O, CRIU, Buildah & Cilium Service Mesh (eBPF)

**Тема исследования:** Автоматическая миграция и восстановление состояния оперативной памяти (RAM) контейнеров между нодами в Kubernetes с использованием эталонного рантайма **CRI-O v1.31**, движка **CRIU**, упаковки чекпоинтов в OCI-образы через **Buildah** и маршрутизации **Cilium Service Mesh (eBPF)**.

### Официальная спецификация Kubernetes (Первоисточник):
* **KEP-2008 (Forensic Container Checkpointing, Beta в K8s 1.30+):** [`kubernetes/enhancements/keps/sig-node/2008-forensic-container-checkpointing`](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/2008-forensic-container-checkpointing)

---

## 1. Архитектура стенда и 4-шаговый конвейер миграции

* **ОС и ядро:** Ubuntu 24.04 LTS (`x86_64`, Linux Kernel 6.8+).
* **Контейнерный рантайм:** **CRI-O v1.31** (`enable_criu_support = true`, `default_runtime = "crun"`) + **Buildah** + **CRIU 3.19+**.
* **Топология кластера (AWS EC2 в одной Availability Zone):**
  * `k8s-master` (`t3.medium`, `10.10.1.10`) — Control Plane, Cilium Ingress Gateway (`80`/`443`), локальный OCI Registry чекпоинтов (`10.10.1.10:5000`).
  * `k8s-worker-1` (`m6i.large`, `10.10.1.21`, 8 GB RAM, выделенные CPU без burst-троттлинга) — Исходный сервер (Source Node).
  * `k8s-worker-2` (`m6i.large`, `10.10.1.22`, 8 GB RAM, выделенные CPU без burst-троттлинга) — Целевой сервер (Target Node).
* **Сетевой стек:** **Cilium 1.16.3** (`kubeProxyReplacement=true`, `clusterPoolIPv4PodCIDRList=10.244.0.0/16`, `l7Proxy=true`, `ingressController.enabled=true`).

### Инженерные детали реализации (`ansible/site.yml` и `manifests/05-live-migration-operator.yaml`):
1. **Доступ к `/var/lib/kubelet/checkpoints/` и `buildah` на исходной ноде:** Оператор развернут как привилегированный **`DaemonSet`** (`hostPID: true`, `hostNetwork: true`, `privileged: true`) и выполняет сборку через `nsenter -t 1 -m -u -i -n` непосредственно на том воркере, где расположен мигрируемый под.
2. **Аутентификация в Kubelet API (`:10250/checkpoint`):** `ServiceAccount` оператора (`live-migration-operator-sa`) привязан через `ClusterRoleBinding` к `cluster-admin` (включая право `nodes/checkpoint`) и передает `Authorization: Bearer <token>` в `POST https://localhost:10250/checkpoint/...`.
3. **Согласование имен, базового образа и Insecure Registry:**
   * Аннотация `io.kubernetes.cri-o.annotations.checkpoint.name=stream-server` совпадает с именем контейнера `stream-server`, а восстановленный под жестко привязывается к целевой ноде через `spec.nodeName`.
   * Базовый образ `docker.io/library/python:3.12-slim` предварительно загружается (`crictl pull`) во все узлы CRI-O на этапе провижининга (`ansible/site.yml`, шаг `1.16`).
   * Локальный реестр `10.10.1.10:5000` прописан с `insecure = true` в `/etc/containers/registries.conf.d/99-local-checkpoint-registry.conf` на всех узлах кластера, а `buildah push` выполняется с `--tls-verify=false`.
4. **Сетевая семантика при смене Pod IP между нодами:** Поскольку стандартный IPAM выделяет восстановленному поду на `k8s-worker-2` новый Pod IP из подсети второй ноды, опция `tcp-established` исключена из `/etc/criu/default.conf`, а демонстрационный сервер использует чистый слушающий сокет `0.0.0.0:8090` (`HTTP/1.0`, `automountServiceAccountToken: false`). Благодаря этому `criu restore` восстанавливает процесс и счетчик в RAM (`STATE["ram_counter"]`) в новом сетевом пространстве имен без конфликта старых TCP-кортежей.

---

## 2. Методология замеров (`scripts/benchmark_migration.py`)

Для получения воспроизводимых статистических данных (медиана `p50` и `p95` на серии из $N=20..30$ прогонов) в репозитории предусмотрен скрипт [`scripts/benchmark_migration.py`](scripts/benchmark_migration.py):
* Внешний поток-пробер опрашивает сервис каждые **20 мс (50 Гц)** и замеряет **реальный даунтайм обслуживания HTTP-трафика** (интервал между последним `HTTP 200` от `worker-1` и первым `HTTP 200` от `worker-2`), а не просто формальный переход пода в статус `Running`.
* Фиксируются 3 контрольные точки счетчика RAM:
  * `counter_at_dump` — значение в момент создания архива CRIU (`POST /checkpoint`);
  * `counter_at_kill` — значение непосредственно перед удалением исходного пода (так как контейнер на `worker-1` продолжает работать во время фаз `Build` и `Push`);
  * `counter_at_restore` — первое значение после `criu restore` на `worker-2` (равное `counter_at_dump`, что доказывает сохранение оперативной памяти и одновременно показывает величину отката за время сборки и пересылки образа).

---

## 3. Быстрый старт (Terraform + Ansible + Benchmark)

```bash
# 1. Создание 3 серверов в AWS (k8s-master, k8s-worker-1, k8s-worker-2)
cd terraform
terraform init
terraform apply -auto-approve

# 2. Установка CRI-O v1.31, CRIU, Buildah, K8s v1.31, Cilium eBPF и приложений
cd ../ansible
ansible-playbook site.yml

# 3. Запуск серии из 20 прогонов миграции с внешним 20ms HTTP-пробером на мастере
ssh -i ../terraform/k8s-research-key.pem ubuntu@<MASTER_PUBLIC_IP> "python3 /home/ubuntu/scripts/benchmark_migration.py --runs 20"
```
