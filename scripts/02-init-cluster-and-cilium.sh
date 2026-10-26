#!/usr/bin/env bash
set -euo pipefail

MASTER_PRIVATE_IP="${1:-10.10.1.10}"

echo "=== [1/4] Инициализация Kubernetes Control Plane (без стандартного kube-proxy для замены на Cilium eBPF) ==="
sudo kubeadm init \
  --apiserver-advertise-address="${MASTER_PRIVATE_IP}" \
  --pod-network-cidr="10.244.0.0/16" \
  --skip-phases=addon/kube-proxy

mkdir -p "$HOME/.kube"
sudo cp -f /etc/kubernetes/admin.conf "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"

echo "=== [2/4] Установка Cilium CLI ==="
CILIUM_CLI_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/cilium-cli/main/stable.txt)
CLI_ARCH=amd64
curl -L --fail --remote-name-all "https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-${CLI_ARCH}.tar.gz"
sudo tar xzvfC "cilium-linux-${CLI_ARCH}.tar.gz" /usr/local/bin
rm -f "cilium-linux-${CLI_ARCH}.tar.gz"

echo "=== [3/4] Установка Cilium Service Mesh с полным eBPF Datapath и Hubble ==="
cilium install \
  --version 1.16.3 \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost="${MASTER_PRIVATE_IP}" \
  --set k8sServicePort=6443 \
  --set bpf.masquerade=true \
  --set l7Proxy=true \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true

echo "=== [4/4] Генерация команды подключения воркеров ==="
kubeadm token create --print-join-command > "$HOME/join-command.sh"
chmod +x "$HOME/join-command.sh"
echo "=== Control Plane и Cilium Service Mesh (eBPF) инициализированы! ==="
