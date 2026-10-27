#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Подготовка идентичного окружения и Network Namespace (migns) на воркерах
# По официальным требованиям CRIU (--tcp-established / Live Migration):
# Имя интерфейса (veth-pod), MAC-адрес и IP (10.244.88.10/24) должны быть
# абсолютно идентичны на Source Node (worker-1) и Target Node (worker-2).
# ==============================================================================

mkdir -p /etc/criu
cat <<EOF > /etc/criu/default.conf
tcp-established
shell-job
ext-unix-sk
EOF
cp -f /etc/criu/default.conf /etc/criu/runc.conf

mkdir -p /opt/criu-operator/checkpoint/pre /opt/criu-operator/checkpoint/dump
hostname > /etc/host_nodename

# Создаем изолированный сетевой неймспейс migns, если он еще не создан
if ! ip netns list | grep -q "^migns"; then
  ip netns add migns
fi

# Очищаем старый veth, если есть, и создаем идентичную пару veth-host <-> veth-pod
ip link del veth-host 2>/dev/null || true
ip link add veth-host type veth peer name veth-pod
ip link set veth-pod netns migns

# Настройка стороны хоста
ip addr add 10.244.88.1/24 dev veth-host 2>/dev/null || true
ip link set veth-host up

# Настройка стороны контейнера (фиксированный MAC и IP 10.244.88.10)
ip netns exec migns ip link set dev lo up
ip netns exec migns ip link set dev veth-pod address 02:42:0a:f4:58:0a
ip netns exec migns ip addr add 10.244.88.10/24 dev veth-pod
ip netns exec migns ip link set dev veth-pod up
ip netns exec migns ip route add default via 10.244.88.1

# Разрешаем пересылку трафика к 10.244.88.10
iptables -P FORWARD ACCEPT || true
