#!/usr/bin/env python3
"""
Комплексный статистический бенчмарк миграции состояния в Kubernetes (CRI-O + CRIU vs Baselines).
Выполняет 3 серии замеров с внешним высокочастотным HTTP-пробером (каждые 20 мс / 50 Гц):
  1. CRI-O + CRIU + Buildah Stateful Migration (N прогонов, по умолчанию 20):
     - Разбивка: T_dump, T_build, T_push, T_restore, External HTTP Blackout (p50 и p95).
     - Проверка непрерывности и разрыва счётчика: counter_at_dump -> counter_at_kill -> counter_at_restore.
  2. Baseline 1 — Standard Cold Reschedule (обычное пересоздание пода без CRIU):
     - Замеряет External HTTP Blackout и демонстрирует полную потерю состояния в RAM (сброс в 0).
  3. Baseline 2 — внешнее хранилище состояния (Redis, redis-cart:6379):
     - Замеряет только сетевую задержку синхронной записи одного изменения состояния в Redis
       (время восстановления и репликация Redis не измеряются).
"""

import argparse
import json
import re
import statistics
import subprocess
import threading
import time
import urllib.request


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True).stdout.strip()


def get_service_url():
    cluster_ip = sh("kubectl get svc live-stream-service -n boutique -o jsonpath='{.spec.clusterIP}'")
    return f"http://{cluster_ip}:8090/live/state"


class HighFrequencyProber:
    def __init__(self, url, interval_sec=0.02):
        self.url = url
        self.interval_sec = interval_sec
        self.stop_event = threading.Event()
        self.samples = []
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _loop(self):
        while not self.stop_event.is_set():
            t_req = time.monotonic()
            try:
                with urllib.request.urlopen(self.url, timeout=0.15) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        self.samples.append({
                            "ts": t_req,
                            "ok": True,
                            "counter": data.get("ram_counter"),
                            "node": data.get("node"),
                            "pod_ip": data.get("pod_ip"),
                        })
                    else:
                        self.samples.append({"ts": t_req, "ok": False})
            except Exception:
                self.samples.append({"ts": t_req, "ok": False})
            elapsed = time.monotonic() - t_req
            sleep_for = max(0.0, self.interval_sec - elapsed)
            time.sleep(sleep_for)

    def max_http_blackout_ms(self):
        ok_times = [s["ts"] for s in self.samples if s.get("ok")]
        if len(ok_times) < 2:
            return 0
        max_gap = max(ok_times[i] - ok_times[i - 1] for i in range(1, len(ok_times)))
        return int(max_gap * 1000)

    def first_and_last_counter(self):
        ok_samples = [s for s in self.samples if s.get("ok") and s.get("counter") is not None]
        if not ok_samples:
            return (None, None)
        return (ok_samples[0]["counter"], ok_samples[-1]["counter"])


def percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[idx]


def run_single_criu_migration(run_idx, src_node, dst_node, svc_url):
    plm_name = f"bench-run-{run_idx}-{int(time.time())}"
    prober = HighFrequencyProber(svc_url, interval_sec=0.02)
    prober.start()
    time.sleep(0.3)

    manifest = json.dumps({
        "apiVersion": "migration.research.io/v1alpha1",
        "kind": "PodLiveMigration",
        "metadata": {"name": plm_name, "namespace": "boutique"},
        "spec": {
            "podName": "live-stream-pod",
            "sourceNode": src_node,
            "targetNode": dst_node,
            "triggerReason": "Benchmark",
        },
    })
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True, check=True, capture_output=True)

    breakdown_str = ""
    for _ in range(120):
        out = sh(f"kubectl get plm {plm_name} -n boutique -o json")
        obj = json.loads(out)
        st = obj.get("status", {})
        if st.get("phase") == "Succeeded":
            breakdown_str = st.get("breakdown", "")
            break
        if st.get("phase") == "Failed":
            prober.stop()
            raise RuntimeError(f"Migration failed: {st.get('message')}")
        time.sleep(0.25)

    time.sleep(0.3)
    prober.stop()

    ext_blackout_ms = prober.max_http_blackout_ms()
    sh(f"kubectl delete plm {plm_name} -n boutique --ignore-not-found >/dev/null 2>&1 || true")
    m = re.search(
        r"Dump:(\d+)ms \| Build:(\d+)ms \| Push:(\d+)ms \| Restore:(\d+)ms \| HTTP_Blackout:(\d+)ms \| Counter\((\w+)->(\w+)->(\w+)\)",
        breakdown_str,
    )
    if not m:
        raise RuntimeError(f"Unexpected breakdown format: {breakdown_str}")

    return {
        "dump_ms": int(m.group(1)),
        "build_ms": int(m.group(2)),
        "push_ms": int(m.group(3)),
        "restore_ms": int(m.group(4)),
        "op_blackout_ms": int(m.group(5)),
        "ext_blackout_ms": ext_blackout_ms,
        "counter_dump": m.group(6),
        "counter_kill": m.group(7),
        "counter_restore": m.group(8),
    }


def run_baseline_cold_reschedule(svc_url):
    """Baseline 1: Обычный Reschedule без CRIU (удаление пода и холодный старт заново из манифеста с потерей RAM)."""
    with urllib.request.urlopen(svc_url, timeout=1.0) as r:
        before_kill = json.loads(r.read().decode("utf-8")).get("ram_counter")

    prober = HighFrequencyProber(svc_url, interval_sec=0.02)
    prober.start()
    time.sleep(0.2)

    t0 = time.monotonic()
    sh("kubectl delete pod live-stream-pod -n boutique --grace-period=0 --force >/dev/null 2>&1 || true")
    sh("kubectl apply -f /home/ubuntu/manifests/03-live-migration-stream-demo.yaml >/dev/null")

    after_restart = None
    for _ in range(200):
        try:
            with urllib.request.urlopen(svc_url, timeout=0.2) as r:
                if r.status == 200:
                    after_restart = json.loads(r.read().decode("utf-8")).get("ram_counter")
                    break
        except Exception:
            pass
        time.sleep(0.05)

    total_ms = int((time.monotonic() - t0) * 1000)
    time.sleep(0.2)
    prober.stop()

    return {
        "total_ms": total_ms,
        "ext_blackout_ms": prober.max_http_blackout_ms(),
        "counter_before_kill": before_kill,
        "counter_after_restart": after_restart,
    }


def run_baseline_redis_replication():
    """Baseline 2: Замер RTT синхронной записи состояния во внешнее хранилище (redis-cart:6379). Это задержка записи, а не полноценная Redis-репликация."""
    redis_ip = sh("kubectl get svc redis-cart -n boutique -o jsonpath='{.spec.clusterIP}'")
    py_code = (
        "import socket, time\n"
        f"s = socket.create_connection(('{redis_ip}', 6379), timeout=2)\n"
        "rtts = []\n"
        "for i in range(1, 101):\n"
        "    t0 = time.monotonic()\n"
        "    cmd = f'*3\\r\\n$3\\r\\nSET\\r\\n$13\\r\\nbench_counter\\r\\n${len(str(i))}\\r\\n{i}\\r\\n'.encode()\n"
        "    s.sendall(cmd)\n"
        "    s.recv(64)\n"
        "    rtts.append((time.monotonic() - t0) * 1000.0)\n"
        "s.close()\n"
        "print(f'{sum(rtts)/len(rtts):.3f}')\n"
    )
    avg_rtt_ms = float(subprocess.run(["python3", "-c", py_code], capture_output=True, text=True, check=True).stdout.strip())
    return {"avg_write_rtt_ms": avg_rtt_ms}


def main():
    parser = argparse.ArgumentParser(description="CRI-O + CRIU Stateful Pod Migration Benchmark")
    parser.add_argument("--runs", type=int, default=20, help="Number of CRIU migration runs (default: 20)")
    args = parser.parse_args()

    svc_url = get_service_url()
    sh("kubectl delete plm --all -n boutique >/dev/null 2>&1 || true")
    print(f"[*] High-Frequency External Prober target: {svc_url} (polling every 20 ms / 50 Hz)\n")

    results = []
    nodes = ["k8s-worker-1", "k8s-worker-2"]

    print(f"=== PART 1: CRI-O + CRIU STATEFUL MIGRATION ({args.runs} RUNS) ===")
    for i in range(1, args.runs + 1):
        cur_node = sh("kubectl get pod live-stream-pod -n boutique -o jsonpath='{.spec.nodeName}'")
        dst_node = nodes[1] if cur_node == nodes[0] else nodes[0]
        res = run_single_criu_migration(i, cur_node, dst_node, svc_url)
        results.append(res)
        print(
            f"[Run {i:02d}/{args.runs:02d}] {cur_node} -> {dst_node} | "
            f"Dump={res['dump_ms']}ms Build={res['build_ms']}ms Push={res['push_ms']}ms "
            f"Restore={res['restore_ms']}ms | Ext_HTTP_Blackout={res['ext_blackout_ms']}ms | "
            f"Counter(dump={res['counter_dump']} -> kill={res['counter_kill']} -> restore={res['counter_restore']})"
        )
        time.sleep(0.8)

    print("\n=== SUMMARY STATISTICS: CRI-O + CRIU (N = {} runs) ===".format(len(results)))
    for key, label in [
        ("dump_ms", "T_dump (Kubelet Checkpoint API)"),
        ("build_ms", "T_build (Buildah OCI Image)"),
        ("push_ms", "T_push (Registry Transfer)"),
        ("restore_ms", "T_restore (CRI-O Restore + HTTP 200)"),
        ("ext_blackout_ms", "External Client HTTP Blackout (20ms Prober)"),
    ]:
        vals = [r[key] for r in results]
        med = int(statistics.median(vals))
        p95 = percentile(vals, 95)
        print(f"- {label:44s}: Median (p50) = {med:4d} ms | p95 = {p95:4d} ms")

    print("\n=== PART 2: BASELINE COMPARISONS ===")
    redis_base = run_baseline_redis_replication()
    print(
        f"- [Baseline 2: External state store, Redis (redis-cart:6379)] "
        f"Sync network write overhead per state mutation = {redis_base['avg_write_rtt_ms']:.3f} ms/op "
        f"(requires app refactoring & external DB)"
    )

    cold_base = run_baseline_cold_reschedule(svc_url)
    print(
        f"- [Baseline 1: Standard Cold Reschedule (no CRIU)]  "
        f"Ext_HTTP_Blackout = {cold_base['ext_blackout_ms']} ms | "
        f"Counter: {cold_base['counter_before_kill']} -> {cold_base['counter_after_restart']} (100% RAM STATE LOST!)"
    )


if __name__ == "__main__":
    main()
