#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/terraform/cluster_ips.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Ошибка: не найден файл ${ENV_FILE}. Сначала выполните 'terraform apply' в папке terraform/."
  exit 1
fi

# shellcheck disable=SC1090
source "${ENV_FILE}"

SSH_OPTS="-i ${SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

echo "=================================================================="
echo "1. Установка базовых пакетов (CRIU, containerd, K8s) на все 3 ноды"
echo "=================================================================="

for HOST_NAME_IP in "k8s-master:${MASTER_PUBLIC_IP}" "k8s-worker-1:${WORKER1_PUBLIC_IP}" "k8s-worker-2:${WORKER2_PUBLIC_IP}"; do
  NODE_NAME="${HOST_NAME_IP%%:*}"
  NODE_IP="${HOST_NAME_IP##*:}"
  echo "---> Настройка ${NODE_NAME} (${NODE_IP})..."
  ssh ${SSH_OPTS} "ubuntu@${NODE_IP}" "sudo hostnamectl set-hostname ${NODE_NAME}"
  scp ${SSH_OPTS} "${SCRIPT_DIR}/01-prepare-node.sh" "ubuntu@${NODE_IP}:/tmp/01-prepare-node.sh"
  ssh ${SSH_OPTS} "ubuntu@${NODE_IP}" "chmod +x /tmp/01-prepare-node.sh && /tmp/01-prepare-node.sh"
done

echo "=================================================================="
echo "2. Инициализация Kubernetes и Cilium Service Mesh (eBPF) на мастере"
echo "=================================================================="
scp ${SSH_OPTS} "${SCRIPT_DIR}/02-init-cluster-and-cilium.sh" "ubuntu@${MASTER_PUBLIC_IP}:/tmp/02-init-cluster-and-cilium.sh"
ssh ${SSH_OPTS} "ubuntu@${MASTER_PUBLIC_IP}" "chmod +x /tmp/02-init-cluster-and-cilium.sh && /tmp/02-init-cluster-and-cilium.sh ${MASTER_PRIVATE_IP}"

JOIN_CMD=$(ssh ${SSH_OPTS} "ubuntu@${MASTER_PUBLIC_IP}" "cat ~/join-command.sh")

echo "=================================================================="
echo "3. Подключение воркеров k8s-worker-1 и k8s-worker-2 к кластеру"
echo "=================================================================="
for WORKER_IP in "${WORKER1_PUBLIC_IP}" "${WORKER2_PUBLIC_IP}"; do
  echo "---> Присоединение воркера ${WORKER_IP}..."
  ssh ${SSH_OPTS} "ubuntu@${WORKER_IP}" "sudo ${JOIN_CMD}"
done

echo "=================================================================="
echo "4. Ожидание готовности Cilium Service Mesh (eBPF) и всех нод..."
echo "=================================================================="
ssh ${SSH_OPTS} "ubuntu@${MASTER_PUBLIC_IP}" "cilium status --wait && kubectl get nodes -o wide"

echo "=================================================================="
echo "Кластер полностью готов к живой миграции подов (CRIU + Cilium eBPF)!"
echo "Вход на мастер: ssh -i ${SSH_KEY} ubuntu@${MASTER_PUBLIC_IP}"
echo "=================================================================="
