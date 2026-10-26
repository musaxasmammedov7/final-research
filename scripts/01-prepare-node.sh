#!/usr/bin/env bash
set -euo pipefail

echo "=== [1/6] Отключение Swap и загрузка модулей ядра Linux (6.8+) ==="
sudo swapoff -a
sudo sed -i '/ swap / s/^\(.*\)$/#\1/g' /etc/fstab

cat <<EOF | sudo tee /etc/modules-load.d/k8s-criu.conf
overlay
br_netfilter
EOF

sudo modprobe overlay
sudo modprobe br_netfilter

echo "=== [2/6] Настройка параметров ядра sysctl для Cilium eBPF и CRIU TCP_REPAIR ==="
cat <<EOF | sudo tee /etc/sysctl.d/99-k8s-criu-ebpf.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
net.ipv4.conf.all.rp_filter         = 0
net.ipv4.conf.default.rp_filter     = 0
# Разрешаем userfaultfd для Lazy Post-Copy миграции памяти в CRIU
vm.unprivileged_userfaultfd         = 1
# Важно для живой миграции TCP: не сбрасывать перенесенные активные TCP-сессии без SYN на новой ноде
net.netfilter.nf_conntrack_tcp_loose = 1
EOF

sudo sysctl --system

echo "=== [3/6] Установка CRIU, crun, containerd, bpftool и сетевых утилит ==="
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  apt-transport-https \
  ca-certificates \
  curl \
  gpg \
  socat \
  conntrack \
  ipset \
  iproute2 \
  nftables \
  criu \
  crun \
  containerd \
  linux-tools-common \
  "linux-tools-$(uname -r)" || true

echo "=== Проверка поддержки CRIU в ядре Linux ==="
sudo criu check || echo "WARNING: criu check returned warnings, continuing..."

echo "=== [4/6] Настройка containerd (SystemdCgroup = true + enable_cri_checkpoint) ==="
sudo mkdir -p /etc/containerd
containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/g' /etc/containerd/config.toml

sudo systemctl restart containerd
sudo systemctl enable containerd

echo "=== [5/6] Установка Kubernetes v1.31 (kubeadm, kubelet, kubectl) ==="
sudo mkdir -p -m 755 /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.31/deb/Release.key | sudo gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.31/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list

sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl

echo "=== [6/6] Включение Feature Gate ContainerCheckpoint=true в kubelet ==="
cat <<EOF | sudo tee /etc/default/kubelet
KUBELET_EXTRA_ARGS="--feature-gates=ContainerCheckpoint=true"
EOF

sudo systemctl daemon-reload
sudo systemctl enable kubelet
echo "=== Узел $(hostname) успешно подготовлен! ==="
