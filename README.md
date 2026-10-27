# Stateful Pod Live Migration in Kubernetes via CRIU & Cilium Service Mesh (eBPF)

**Тема исследования:** Бесшовная живая миграция stateful-контейнеров между серверами в Kubernetes с сохранением активных TCP-соединений (`TCP_REPAIR`), состояния оперативной памяти (Pre-Copy / Checkpoint) и маршрутизации через **Cilium Service Mesh (eBPF)** на базе официальных сценариев **Kubernetes WG Checkpoint/Restore (KEP-2008 & KEP-5823)**.

---

## 1. Архитектура стенда

* **ОС и ядро:** Ubuntu 24.04 LTS (`x86_64`, Linux Kernel 6.8+ с поддержкой `TCP_REPAIR`, `PAGEMAP_SCAN`, `userfaultfd`, `nf_conntrack_tcp_loose=1` и `eBPF`).
* **Топология кластера (AWS EC2 в одной Availability Zone, задержка `< 0.3 мс`):**
  * `k8s-master` (`t3.medium`, `10.10.1.10`) — Control Plane, Cilium Ingress Gateway (`80`/`443`).
  * `k8s-worker-1` (`t3.large`, `10.10.1.21`, 8 GB RAM) — Исходный сервер (Source Node).
  * `k8s-worker-2` (`t3.large`, `10.10.1.22`, 8 GB RAM) — Целевой сервер (Target Node).
* **Сетевой стек и Service Mesh:** **Cilium 1.16.3** в режиме `kubeProxyReplacement=true` (чистый eBPF datapath без `iptables` и без sidecar-контейнеров внутри подов) + **Cilium Ingress Controller** + **Hubble**.
* **Домен и шифрование:** Автоматический выпуск TLS-сертификатов Let's Encrypt через **cert-manager** для домена `https://musa-research.duckdns.org`.
* **Автономный Оператор (`kind: PodLiveMigration`):** Собственный CRD и контроллер внутри Kubernetes, который автоматически мониторит 3 официальных триггера из **KEP-5823**:
  1. `NodeMaintenance` (`node.spec.unschedulable == true` при `kubectl cordon` / `kubectl drain`);
  2. `SpotInstancePreemption` (сигнал отзыва Spot-сервера);
  3. `ClusterConsolidation` (уплотнение подов для освобождения недогруженных нод).

---

## 2. Структура репозитория

```text
final-research/
├── terraform/                                    # Инфраструктура AWS EC2 (IaC)
│   ├── main.tf                                   # VPC, Subnet, SG (22/80/443), 3x Ubuntu 24.04, source_dest_check=false
│   ├── variables.tf                              # Переменные региона, типов инстансов и DuckDNS
│   └── outputs.tf                                # Вывод публичных IP-адресов и SSH-команды
├── ansible/                                      # Конфигурация серверов и развертывание кластера
│   ├── ansible.cfg                               # Конфигурация SSH pipelining и инвентаря
│   └── site.yml                                  # Playbook: sysctl, CRIU, containerd, K8s v1.31, Cilium eBPF, cert-manager
├── manifests/                                    # Манифесты Kubernetes
│   ├── 01-cert-manager-issuer.yaml               # ClusterIssuer Let's Encrypt для Cilium Ingress
│   ├── 02-google-online-boutique.yaml            # 11 микросервисов Google Online Boutique (gRPC + redis-cart в RAM)
│   ├── 03-live-migration-stream-demo.yaml        # Визуализатор живой миграции (/live) с непрерывным TCP-потоком
│   ├── 04-cilium-ingress-https.yaml              # Маршрутизация Cilium Ingress (HTTPS -> "/" и "/live")
│   └── 05-live-migration-operator.yaml           # CRD (kind: PodLiveMigration) + Автономный Оператор миграции
└── README.md
```

---

## 3. Быстрый запуск с нуля

### Шаг 1. Настройка AWS CLI
```bash
aws configure
```

### Шаг 2. Создание 3 серверов в AWS через Terraform
```bash
cd terraform
terraform init
terraform apply -auto-approve
```

### Шаг 3. Полная настройка кластера, CRIU, Cilium eBPF и приложений через Ansible
```bash
cd ../ansible
ansible-playbook site.yml
```

### Шаг 4. Проверка и демонстрация автоматической миграции
1. Откройте магазин **Google Online Boutique**: `https://musa-research.duckdns.org/`
2. Откройте **Визуализатор живой миграции**: `https://musa-research.duckdns.org/live`
3. На мастер-ноде вызовите любой из 3 официальных триггеров **KEP-5823**, например вывод `k8s-worker-1` на обслуживание:
   ```bash
   kubectl cordon k8s-worker-1
   ```
4. Проверьте созданный Оператором ресурс `PodLiveMigration`:
   ```bash
   kubectl get podlivemigrations -n boutique
   ```
