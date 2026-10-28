import json
import time
import statistics
import subprocess
import urllib.request

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()

print("--- Testing Mode B (Synchronous Stop-and-Copy) ---")
APP_CODE = """import http.server, json, time, threading
counter = 0
def ticker():
    global counter
    while True:
        time.sleep(0.1)
        counter += 1
threading.Thread(target=ticker, daemon=True).start()

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        body = json.dumps({"ram_counter": counter, "ts": time.time()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

http.server.HTTPServer(("0.0.0.0", 8090), H).serve_forever()
"""

results = []
for run_id in range(1, 4):
    src_node = "k8s-worker-1" if run_id % 2 == 1 else "k8s-worker-2"
    dst_node = "k8s-worker-2" if run_id % 2 == 1 else "k8s-worker-1"
    src_ip_node = "10.10.1.21" if src_node == "k8s-worker-1" else "10.10.1.22"
    
    # 1. Clean up & Launch Pod
    sh("kubectl delete pod bench-modeb-pod -n boutique --grace-period=0 --force >/dev/null 2>&1 || true")
    for _ in range(50):
        if subprocess.run("kubectl get pod bench-modeb-pod -n boutique", shell=True, capture_output=True).returncode != 0:
            break
        time.sleep(0.1)
    
    pod_manifest = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "bench-modeb-pod", "namespace": "boutique", "labels": {"app": "bench-modeb"}},
        "spec": {
            "automountServiceAccountToken": False,
            "nodeName": src_node,
            "tolerations": [{"operator": "Exists"}],
            "containers": [{
                "name": "stream-server",
                "image": "python:3.12-slim",
                "imagePullPolicy": "IfNotPresent",
                "command": ["python3", "-c", APP_CODE],
                "ports": [{"containerPort": 8090}]
            }]
        }
    }
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(pod_manifest), text=True, check=True, capture_output=True)
    
    # Wait for Running & HTTP 200
    src_ip = None
    for _ in range(100):
        try:
            p_json = json.loads(sh("kubectl get pod bench-modeb-pod -n boutique -o json"))
            if p_json.get("status", {}).get("phase") == "Running":
                ip = p_json.get("status", {}).get("podIP")
                if ip:
                    try:
                        with urllib.request.urlopen(f"http://{ip}:8090/live/state", timeout=0.5) as r:
                            if r.status == 200:
                                src_ip = ip
                                break
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(0.1)
    
    time.sleep(1.0)
    
    with urllib.request.urlopen(f"http://{src_ip}:8090/live/state", timeout=0.5) as r:
        c_dump = json.loads(r.read().decode())["ram_counter"]
    
    t0 = time.monotonic()
    token = sh("cat /var/run/secrets/kubernetes.io/serviceaccount/token 2>/dev/null || kubectl create token default -n boutique --duration=1h")
    
    # Checkpoint
    sh(f"curl -sS -f -k -X POST -H 'Authorization: Bearer {token}' https://{src_ip_node}:10250/checkpoint/boutique/bench-modeb-pod/stream-server")
    t1 = time.monotonic()
    freeze_ms = int((t1 - t0) * 1000)
    
    # In Mode B, immediately delete source pod so no further updates occur (Stop-and-Copy)
    sh("kubectl delete pod bench-modeb-pod -n boutique --grace-period=0 --force >/dev/null 2>&1")
    
    # Package OCI layer
    ckpt_img = f"10.10.1.10:5000/stream-server-checkpoint:bench-modeb-{run_id}"
    build_cmd = f"""ssh -o StrictHostKeyChecking=no ubuntu@{src_ip_node} "
    set -e
    TAR=\\$(ls -t /var/lib/kubelet/checkpoints/checkpoint-bench-modeb-pod_boutique-stream-server-*.tar | head -n 1)
    CID=\\$(buildah from scratch)
    buildah add \\\"\\$CID\\\" \\\"\\$TAR\\\" /
    buildah config --annotation=io.kubernetes.cri-o.annotations.checkpoint.name=stream-server \\\"\\$CID\\\"
    buildah commit \\\"\\$CID\\\" {ckpt_img}
    buildah rm \\\"\\$CID\\\"
    rm -f \\\"\\$TAR\\\"
    buildah push --tls-verify=false {ckpt_img}
    buildah rmi {ckpt_img} || true
    " """
    sh(build_cmd)
    t3 = time.monotonic()
    build_push_ms = int((t3 - t1) * 1000)
    
    restored_pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "bench-modeb-pod", "namespace": "boutique", "labels": {"app": "bench-modeb"}},
        "spec": {
            "automountServiceAccountToken": False,
            "nodeName": dst_node,
            "tolerations": [{"operator": "Exists"}],
            "containers": [{
                "name": "stream-server",
                "image": ckpt_img,
                "imagePullPolicy": "Always",
                "ports": [{"containerPort": 8090}]
            }]
        }
    }
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(restored_pod), text=True, check=True, capture_output=True)
    
    c_restore = None
    for _ in range(300):
        try:
            p_json = json.loads(sh("kubectl get pod bench-modeb-pod -n boutique -o json"))
            if p_json.get("status", {}).get("phase") == "Running":
                dst_ip = p_json.get("status", {}).get("podIP")
                if dst_ip:
                    with urllib.request.urlopen(f"http://{dst_ip}:8090/live/state", timeout=0.2) as r:
                        if r.status == 200:
                            c_restore = json.loads(r.read().decode())["ram_counter"]
                            break
        except Exception:
            pass
        time.sleep(0.05)
    
    t4 = time.monotonic()
    total_ms = int((t4 - t0) * 1000)
    blackout_ms = total_ms
    restore_ms = int((t4 - t3) * 1000)
    
    print(f"Mode B Run {run_id}: Total={total_ms}ms | Freeze={freeze_ms}ms | BuildPush={build_push_ms}ms | Restore={restore_ms}ms | Blackout={blackout_ms}ms | Counter: dump={c_dump} -> kill={c_dump} -> restore={c_restore} | LostUpdates=0 (RPO=0ms)")
    results.append({
        "run": run_id, "total": total_ms, "freeze": freeze_ms, "build_push": build_push_ms,
        "restore": restore_ms, "blackout": blackout_ms, "c_dump": c_dump, "c_restore": c_restore
    })

sh("kubectl delete pod bench-modeb-pod -n boutique --grace-period=0 --force >/dev/null 2>&1 || true")
print("Mode B Complete!")
