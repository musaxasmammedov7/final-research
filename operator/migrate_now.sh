#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ДВИЖОК ЖИВОЙ МИГРАЦИИ КОНТЕЙНЕРА (CRIU Pre-Copy + Dump + Route Switch)
# Измеряет реальное время каждого этапа в миллисекундах без выдуманных цифр.
# ==============================================================================

CURRENT_NODE=$(hostname)
if [[ "${CURRENT_NODE}" == "k8s-worker-1" ]]; then
  SOURCE_IP="10.10.1.21"
  TARGET_NODE="k8s-worker-2"
  TARGET_IP="10.10.1.22"
else
  SOURCE_IP="10.10.1.22"
  TARGET_NODE="k8s-worker-1"
  TARGET_IP="10.10.1.21"
fi

MASTER_IP="10.10.1.10"
SSH_OPTS="-i /root/.ssh/id_rsa -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
WORK_DIR="/opt/criu-operator"

# Находим PID процесса stateful_app.py внутри неймспейса
APP_PID=$(pgrep -f "/opt/criu-operator/stateful_app.py" | head -n 1 || true)
if [[ -z "${APP_PID}" ]]; then
  echo "Процесс stateful_app.py не найден на ${CURRENT_NODE}"
  exit 1
fi

rm -rf "${WORK_DIR}/checkpoint/pre" "${WORK_DIR}/checkpoint/dump"
mkdir -p "${WORK_DIR}/checkpoint/pre" "${WORK_DIR}/checkpoint/dump"
ssh ${SSH_OPTS} "root@${TARGET_IP}" "rm -rf ${WORK_DIR}/checkpoint && mkdir -p ${WORK_DIR}/checkpoint/pre ${WORK_DIR}/checkpoint/dump && pkill -9 -f stateful_app.py || true"

# ------------------------------------------------------------------------------
# ШАГ 1: Iterative Pre-Copy (criu pre-dump) — копируем память БЕЗ остановки процесса!
# ------------------------------------------------------------------------------
T_PRE_START=$(date +%s%3N)
ip netns exec migns criu pre-dump \
  -t "${APP_PID}" \
  --images-dir "${WORK_DIR}/checkpoint/pre" \
  --track-mem \
  --shell-job

rsync -az -e "ssh ${SSH_OPTS}" "${WORK_DIR}/checkpoint/pre/" "root@${TARGET_IP}:${WORK_DIR}/checkpoint/pre/"
T_PRE_END=$(date +%s%3N)
PRE_DUMP_MS=$((T_PRE_END - T_PRE_START))
PRE_SIZE_KB=$(du -sk "${WORK_DIR}/checkpoint/pre" | awk '{print $1}')

# ------------------------------------------------------------------------------
# ШАГ 2: Заморозка (Freeze), финальный дельта-дамп CRIU и перенос на Target Node
# ------------------------------------------------------------------------------
T_FREEZE_START=$(date +%s%3N)

# Блокируем отправку TCP RST во время переноса сокета
iptables -I OUTPUT -s 10.244.88.10 -p tcp --tcp-flags RST RST -j DROP || true

ip netns exec migns criu dump \
  -t "${APP_PID}" \
  --images-dir "${WORK_DIR}/checkpoint/dump" \
  --prev-images-dir "../pre" \
  --tcp-established \
  --shell-job

rsync -az -e "ssh ${SSH_OPTS}" "${WORK_DIR}/checkpoint/dump/" "root@${TARGET_IP}:${WORK_DIR}/checkpoint/dump/"
DUMP_SIZE_KB=$(du -sk "${WORK_DIR}/checkpoint/dump" | awk '{print $1}')

# ------------------------------------------------------------------------------
# ШАГ 3: Восстановление процесса в ядре Target Node (criu restore) и переключение маршрута
# ------------------------------------------------------------------------------
T_RESTORE_START=$(date +%s%3N)
ssh ${SSH_OPTS} "root@${TARGET_IP}" "ip netns exec migns criu restore -d --images-dir ${WORK_DIR}/checkpoint/dump --tcp-established --shell-job"

# Атомарное переключение маршрута /32 для IP 10.244.88.10 на новую ноду
ssh ${SSH_OPTS} "root@${MASTER_IP}" "ip route replace 10.244.88.10/32 via ${TARGET_IP}"
ssh ${SSH_OPTS} "root@${TARGET_IP}" "ip route replace 10.244.88.10/32 dev veth-host"
ip route replace 10.244.88.10/32 via "${TARGET_IP}"

iptables -D OUTPUT -s 10.244.88.10 -p tcp --tcp-flags RST RST -j DROP 2>/dev/null || true

T_FREEZE_END=$(date +%s%3N)
RESTORE_MS=$((T_FREEZE_END - T_RESTORE_START))
TOTAL_FREEZE_MS=$((T_FREEZE_END - T_FREEZE_START))

# Записываем реальные измеренные метрики в JSON на целевой ноде
cat <<EOF | ssh ${SSH_OPTS} "root@${TARGET_IP}" "cat > /var/run/migration_metrics.json"
{
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "direction": "${CURRENT_NODE} (${SOURCE_IP}) ---> ${TARGET_NODE} (${TARGET_IP})",
  "pod_ip_preserved": "10.244.88.10/32",
  "pre_copy_duration_ms": ${PRE_DUMP_MS},
  "pre_copy_ram_transferred_kb": ${PRE_SIZE_KB},
  "final_delta_ram_transferred_kb": ${DUMP_SIZE_KB},
  "restore_and_route_switch_ms": ${RESTORE_MS},
  "total_application_freeze_ms": ${TOTAL_FREEZE_MS}
}
EOF
