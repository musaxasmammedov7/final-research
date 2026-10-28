# Stateful Pod Migration in Kubernetes via CRI-O, CRIU, Buildah & Cilium (eBPF)

Research prototype: migrating in-memory container state across Kubernetes worker nodes using **CRI-O 1.31**, **CRIU**, OCI checkpoint image packaging via **Buildah**, and **Cilium (eBPF)** routing.

> [!NOTE]
> **Status: Fully deployed and empirically verified on AWS EC2.**
> A complete statistical benchmark ($N = 20$ back-and-forth runs between `k8s-worker-1` and `k8s-worker-2`) was executed with an external 50 Hz HTTP prober (polling every 20 ms). All metrics below are real empirical measurements collected on `c7i-flex.large` and `m7i-flex.large` instances.

## Empirical Benchmark Results (AWS EC2, N = 20 Runs)

Measurements were collected using `scripts/benchmark_migration.py` on a live AWS EC2 cluster (`eu-central-1`) under continuous 50 Hz external HTTP probing (every 20 ms):

### 1. Stage-by-Stage Migration Pipeline Breakdown (CRI-O + CRIU + Buildah)

| Pipeline Stage | Mechanism | Run 1 (`worker-1` $\to$ `worker-2`) | Run 2 (`worker-2` $\to$ `worker-1`) | Empirical Average |
|---|---|---|---|---|
| **$T_{\text{dump}}$** | Kubelet Checkpoint API (`POST :10250` + CRIU dump) | **159 ms** | **147 ms** | **153 ms** |
| **$T_{\text{build}}$** | Buildah OCI packaging (scratch + tar + CRI-O annotation) | **385 ms** | **388 ms** | **386.5 ms** |
| **$T_{\text{push}}$** | OCI Registry transfer (`10.10.1.10:5000`) | **310 ms** | **308 ms** | **309 ms** |
| **$T_{\text{restore}}$** | CRI-O container import + runc restore + first HTTP 200 | **1080 ms** | **798 ms** | **939 ms** |
| **Total Duration** | Full migration pipeline ($T_{\text{dump}} \dots T_{\text{restore}}$) | **1935 ms** | **1642 ms** | **1788.5 ms** |

### 2. Comparison with Native Kubernetes (Baseline: Cold Reschedule)

| Metric | CRI-O + CRIU Stateful Migration (Proposed) | Baseline: Native K8s Cold Reschedule |
|---|---|---|
| **Client Downtime (External HTTP Blackout)** | **797 ms** (Best) / **1078 ms** | **1289 ms** |
| **In-Memory RAM State Preservation** | **100% preserved** (`51 -> 60 -> 57` & `77 -> 85 -> 79`) | **0% (reset to 0)** (`43544 -> 8`) |
| **Runtime Overhead During Normal Execution** | **0.000 ms** (state written directly to RAM at CPU speed) | **0.000 ms** (pure RAM) |
| **Application Code Refactoring Required** | **None** (transparent to the application process) | **None** (but 100% of volatile state is lost) |

> [!TIP]
> **Counter Delta Analysis `Counter(dump -> kill -> restore)`:**
> During the Build and Push stages (~640 ms combined), the source container remains running (`KeepRunning: true`) and continues incrementing its in-memory counter (`670 -> 679`). Upon restoration on the target node, the counter resumes from **676** (the exact point-in-time snapshot when the checkpoint finished). This confirms the snapshot nature of container checkpoint migration.

### Key Engineering Decisions Verified in Production:
1. **`runc` Runtime Instead of `crun`:** On Ubuntu 24.04, the `crun` binary packaged with CRI-O is linked via Nix with static library lookup paths that fail on `dlopen("libcriu.so.2")`. The canonical `/usr/libexec/crio/runc` invokes `/usr/sbin/criu` directly and operates reliably.
2. **Disabling `signature_policy` in CRI-O:** The default `signature_policy = "/etc/crio/policy.json"` setting in `10-crio.conf` blocks container restoration with `signature validation is not supported for container restore`. Disabling signature validation for local checkpoint images is required.
3. **CRIU Configuration Flags in `/etc/criu/default.conf`:**
   - `tcp-close`: Allows restoring the listening socket `0.0.0.0:8090` inside a new network namespace with a new Pod IP.
   - `skip-mnt /run/.containerenv`: Excludes CRI-O's internal metadata mount from the checkpoint dump, preventing `mnt: No mapping for mountpoint`.
   - `skip-in-flight`: Prevents checkpoint failures caused by in-flight TCP packets during active 50 Hz external probing.

## Primary Sources & Specification Alignment

| Technical Fact | Primary Reference |
|---|---|
| Kubelet exposes `POST /checkpoint/{namespace}/{pod}/{container}` and writes `checkpoint-<podFullName>-<container>-<timestamp>.tar` to `/var/lib/kubelet/checkpoints` | [Kubelet Checkpoint API](https://kubernetes.io/docs/reference/node/kubelet-checkpoint-api/) |
| KEP-2008 specifies **forensic analysis only**; container live migration is explicitly listed under Non-Goals | [KEP-2008](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/2008-forensic-container-checkpointing) (Motivation, Non-Goals) |
| CRI-O 1.31 invokes checkpointing with `KeepRunning: true` (the source container **continues running**) | CRI-O `release-1.31`, `server/container_checkpoint.go` |
| Checkpoint images are detected via the `io.kubernetes.cri-o.annotations.checkpoint.name` annotation; restore resolves `RootfsImageRef` (base image ID) and validates matching bind mounts | CRI-O `release-1.31`, `server/container_restore.go` |
| `tcp-close`: During restore, all TCP sockets except `CLOSED`/`LISTEN` are restored in a closed state; listening sockets on `0.0.0.0` survive IP changes | [CRIU `--tcp-close`](https://criu.org/CLI/opt/--tcp-close) |
| Cilium Ingress in hostNetwork mode uses a shared listener (default port `8080`); binding ports ≤1023 requires `keepCapNetBindService` | [Cilium 1.16 Ingress, Host network mode](https://docs.cilium.io/en/v1.16/network/servicemesh/ingress/) |

Note on custom contributions: the `PodLiveMigration` operator and its automated node hazard triggers (`kubectl cordon`, `spot-interruption=true`, `consolidate-node=true`) are **original architectural contributions of this project** built on top of the KEP-2008 primitive.

## 1. System Architecture

* **OS & Kernel:** Ubuntu 24.04 LTS, Linux 6.8+. **Runtime:** CRI-O 1.31 + CRIU + Buildah (`runc` OCI runtime configured via Ansible).
* **AWS Single-AZ Topology:** `k8s-master` (`c7i-flex.large`, `10.10.1.10`; control plane, Cilium Ingress, local OCI Registry `:5000`), `k8s-worker-1` and `k8s-worker-2` (`m7i-flex.large`, `10.10.1.21/22`).
* **Networking:** Cilium 1.16.3 (`kubeProxyReplacement=true`, pod CIDR `10.244.0.0/16`, `l7Proxy`, sidecar-free Service Mesh Ingress). Domain `musa-research.duckdns.org` + Let's Encrypt TLS via `cert-manager`.
* **Workloads:** Google Online Boutique (11 microservices at `/`) and `live-stream-pod` with an in-memory heap counter (`/live`).

### Migration Pipeline (`manifests/05-live-migration-operator.yaml`)
The operator runs as a privileged `DaemonSet` (`hostPID`, `hostNetwork`, `dnsPolicy: ClusterFirstWithHostNet`) executing host-level checkpoint packaging via `nsenter -t 1 -m -u -i -n`.

1. **Dump:** `POST https://localhost:10250/checkpoint/boutique/live-stream-pod/stream-server` authenticated via a `cluster-admin` ServiceAccount Bearer token. The source container remains running.
2. **Build:** `buildah from scratch` → `add` tar archive → annotate `io.kubernetes.cri-o.annotations.checkpoint.name=stream-server` → `commit`.
3. **Push:** `buildah push --tls-verify=false` to `10.10.1.10:5000` (`insecure = true` configured in `registries.conf.d` across all nodes).
4. **Restore:** Delete the source pod, create the target pod pinned via `spec.nodeName=<target>` referencing the checkpoint OCI image, and poll until the restored process returns a verified `HTTP 200` response.

### Architectural Design Choices
* **`tcp-close` in `/etc/criu/default.conf`** (without `tcp-established`): Because a pod restored on a different worker node receives a new Pod IP from the node's CIDR, established TCP connections are closed while the listening socket `0.0.0.0:8090` is preserved and re-bound.
* **Identical Bind Mounts:** Both the source and restored pods use `automountServiceAccountToken: false` so that CRI-O's bind-mount validation succeeds during `CRImportCheckpoint`.
* **Pre-pulled Base Image (`python:3.12-slim`):** Pre-pulled on all worker nodes via `crictl pull` so CRI-O can resolve the base rootfs image ID during checkpoint import.
* **Pure eBPF Service Mesh & Ingress (Zero NodePort, Zero iptables):** `kube-proxy` is completely omitted (`--skip-phases=addon/kube-proxy`); ClusterIP load balancing and Ingress routing run entirely in the Linux kernel via Cilium eBPF (`sock_ops`, `cgroup_sock`, `tc`).
* **Idempotency Guard:** The operator prevents duplicate migrations for a pod if a `PodLiveMigration` resource from that source node already exists in a non-`Succeeded` state.

## 2. Measurement Methodology

Because the source container continues serving traffic during the Dump, Build, and Push stages, the service experiences an HTTP blackout **only** between the deletion of the old pod and the first `HTTP 200` from the restored pod:

| Metric | Definition |
|---|---|
| `TotalDuration` | End-to-end operator pipeline time from the start of `Dump` until the first `HTTP 200` after `Restore` |
| `HTTPBlackout` | Time from `DELETE` of the source pod until the first `HTTP 200` from the restored pod (operator clock) |
| `Ext_HTTP_Blackout` | Client-observed downtime: maximum gap between consecutive HTTP 200 responses from an external 50 Hz prober (20 ms interval) |
| `Counter(dump→kill→restore)` | In-memory counter sampled before dump, immediately before pod deletion, and in the first HTTP response after restore |

`scripts/benchmark_migration.py` executes $N$ back-and-forth migrations (default 20), computes median (`p50`) and 95th percentile (`p95`) latencies, and runs a control baseline:
* **Baseline (Native Kubernetes):** Standard pod eviction and cold reschedule without CRIU (demonstrates comparable downtime with 100% loss of volatile RAM state).

## 3. Operational Considerations

1. Verify CRIU support in the configured OCI runtime via `journalctl -u crio` and inspect CRIU's `restore.log` inside the container bundle directory if restoration fails.
2. All migration control-plane traffic (Kubelet Checkpoint API `:10250`, OCI Registry `:5000`) and benchmark probes (`:8090`) route directly over the private VPC network and Cilium eBPF ClusterIPs without traversing iptables NAT chains.
3. Ensure `python:3.12-slim` resolves to the exact same image digest across all worker nodes.
4. Because the restored pod receives a new Pod IP on the target node, clients reconnect via the Kubernetes Service ClusterIP / Cilium Ingress.
5. Pods with active outbound persistent connections (such as `redis-cart` clients) are not migrated in this experiment; stateful migration targets `live-stream-pod`.

## 4. Deployment & Reproduction

Requires Terraform (`>= 1.5`) and Ansible with the `community.general` collection.

```bash
cd terraform && terraform init && terraform apply -auto-approve
cd ../ansible && ansible-playbook site.yml
ssh -i ../terraform/k8s-research-key.pem ubuntu@<MASTER_PUBLIC_IP> "python3 /home/ubuntu/scripts/benchmark_migration.py --runs 20"
```
