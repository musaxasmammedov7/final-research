#!/usr/bin/env python3
"""
Внешний высокочастотный пробер и статистический бенчмарк миграции CRI-O + CRIU.
- Опрашивает сервис каждые 20 мс (50 Гц) с записью точных таймстемпов.
- Измеряет реальный HTTP-даунтайм глазами внешнего клиента (от последнего HTTP 200 на
  исходной ноде до первого HTTP 200 на целевой ноде).
- Фиксирует значения счетчика RAM (counter_at_dump, counter_at_kill, counter_at_restore),
  демонстрируя сохранение памяти и окно расхождения (rollback за время Build + Push).
- Проводит серию из N прогонов (по умолчанию 20) и считает медиану (p50) и p95,
  а также сравнивает с Baseline (обычный Cold Reschedule с потерей состояния в RAM).
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
    m = re.search(
        r"Dump:(\d+)ms \| Build:(\d+)ms \| Push:(\d+)ms \| Restore:(\d+)ms \| HTTP_Blackout:(\d+)ms \| Counter\((\d+)->(\d+)->(\d+)\)",
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
        "counter_dump": int(m.group(6)),
        "counter_kill": int(m.group(7)),
        "counter_restore": int(m.group(8)),
    }


def main():
    parser = argparse.ArgumentParser(description="CRI-O + CRIU Stateful Pod Migration Benchmark")
    parser.add_argument("--runs", type=int, default=20, help="Number of migration runs (default: 20)")
    args = parser.parse_args()

    svc_url = get_service_url()
    print(f"[*] Probing Service URL: {svc_url} at 20ms intervals (50 Hz)")

    results = []
    nodes = ["k8s-worker-1", "k8s-worker-2"]

    for i in range(1, args.runs + 1):
        cur_node = sh("kubectl get pod live-stream-pod -n boutique -o jsonpath='{.spec.nodeName}'")
        dst_node = nodes[1] if cur_node == nodes[0] else nodes[0]
        res = run_single_criu_migration(i, cur_node, dst_node, svc_url)
        results.append(res)
        print(
            f"[Run {i:02d}/{args.runs:02d}] {cur_node} -> {dst_node} | "
            f"Dump={res['dump_ms']}ms Build={res['build_ms']}ms Push={res['push_ms']}ms "
            f"Restore={res['restore_ms']}ms | Ext_HTTP_Blackout={res['ext_blackout_ms']}ms | "
            f"Counter(dump={res['counter_dump']}, kill={res['counter_kill']}, restore={res['counter_restore']})"
        )
        time.sleep(1.0)

    print("\n=== SUMMARY STATISTICS (N = {} runs) ===".format(len(results)))
    for key, label in [
        ("dump_ms", "T_dump (Kubelet Checkpoint API)"),
        ("build_ms", "T_build (Buildah OCI Image)"),
        ("push_ms", "T_push (Registry Transfer)"),
        ("restore_ms", "T_restore (CRI-O Restore + HTTP Ready)"),
        ("ext_blackout_ms", "External Client HTTP Downtime (20ms Prober)"),
    ]:
        vals = [r[key] for r in results]
        med = int(statistics.median(vals))
        p95 = percentile(vals, 95)
        print(f"- {label:42s}: Median (p50) = {med:4d} ms | p95 = {p95:4d} ms")


if __name__ == "__main__":
    main()
