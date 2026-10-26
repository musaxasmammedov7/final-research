# Stateful Pod Live Migration in Kubernetes via CRIU & Cilium Service Mesh (eBPF)

**Тема исследования:** Бесшовная живая миграция stateful-контейнеров между серверами в Kubernetes с сохранением активных TCP-соединений, состояния памяти (Pre-Copy / Lazy Post-Copy `userfaultfd`) и IP-маршрутизации через **Cilium Service Mesh (eBPF)**.

---

## Архитектура стенда

* **ОС и ядро:** Ubuntu 24.04 LTS (`x86_64`, Linux Kernel 6.8+ с поддержкой `TCP_REPAIR`, `PAGEMAP_SCAN`, `userfaultfd` и `eBPF`).
* **Топология кластера (AWS EC2 в одной Availability Zone):**
  * `k8s-master` (`t3.medium`, `10.10.1.10`) — Control Plane, генератор нагрузки и клиентских TCP-сессий.
  * `k8s-worker-1` (`t3.large`, `10.10.1.21`, 8 GB RAM) — Исходный сервер (Source Node).
  * `k8s-worker-2` (`t3.large`, `10.10.1.22`, 8 GB RAM) — Целевой сервер (Target Node).
* **Сетевой стек и Service Mesh:** **Cilium 1.16+** в режиме `kubeProxyReplacement=true` (чистый eBPF datapath без `iptables` и без громоздких sidecar-контейнеров внутри подов) + **Hubble**.
* **Движок чекпоинтов и миграции:** `CRIU 3.19+`, `containerd` / `crun`, Kubernetes `ContainerCheckpoint` API + собственный оператор живой миграции.

---

## Быстрый старт (Развертывание инфраструктуры)

### Шаг 1. Настройка доступа к AWS на вашем компьютере
Убедитесь, что у вас настроен AWS CLI:
```bash
aws configure
```
*(Введите ваш `AWS Access Key ID`, `AWS Secret Access Key` и регион, например `eu-central-1`).*

### Шаг 2. Создание 3 серверов через Terraform
```bash
cd terraform
terraform init
terraform apply -auto-approve
```
Что сделает Terraform автоматически:
1. Создаст отдельную сеть VPC (`10.10.0.0/16`) и подсеть в одной зоне доступности (задержка между серверами `< 0.3 мс`).
2. Сгенерирует SSH-ключ и сохранит его в `terraform/k8s-research-key.pem`.
3. Создаст 3 сервера Ubuntu 24.04 и **автоматически отключит на них `source_dest_check`**, чтобы AWS не блокировал миграцию IP-адресов подов.
4. Запишет все IP-адреса в файл `terraform/cluster_ips.env`.

### Шаг 3. Автоматическая установка Kubernetes, CRIU и Cilium Service Mesh (eBPF)
Одной командой запустите полную настройку всех 3 серверов по SSH:
```bash
./scripts/bootstrap-all.sh
```

---

## Структура репозитория

```text
final-research/
├── terraform/                  # Инфраструктура AWS EC2 (VPC, SG, 3x Ubuntu 24.04, SSH-ключи)
│   ├── main.tf
│   ├── variables.tf
│   └── outputs.tf
├── scripts/                    # Автоматическая настройка ядра, CRIU, K8s и Cilium eBPF
│   ├── 01-prepare-node.sh
│   ├── 02-init-cluster-and-cilium.sh
│   └── bootstrap-all.sh
└── README.md
```
