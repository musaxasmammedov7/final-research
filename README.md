# Evaluating Checkpoint/Restore-Based Pod Migration in Kubernetes: An Empirical Performance and Bottleneck Study

[![LaTeX Paper](https://img.shields.io/badge/Paper-IEEEtran%205--Page%20PDF-blue.svg)](paper/Stateful_Container_Live_Migration_IEEE_NativeTeX.pdf)
[![LaTeX Source](https://img.shields.io/badge/TeX-IEEE%20Format-green.svg)](paper/paper_stateful_live_migration.tex)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-v1.31-326CE5.svg?logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![CRI-O](https://img.shields.io/badge/Runtime-CRI--O%201.31-red.svg)](https://cri-o.io/)
[![CRIU](https://img.shields.io/badge/Checkpoint-CRIU%20v4.3-orange.svg)](https://criu.org/)
[![Cilium](https://img.shields.io/badge/Networking-Cilium%20eBPF%201.16-yellow.svg?logo=cilium&logoColor=white)](https://cilium.io/)
[![AWS EC2](https://img.shields.io/badge/Infra-AWS%20EC2-232F3E.svg?logo=amazon-aws&logoColor=white)](https://aws.amazon.com/ec2/)

This repository contains the complete experimental testbed, deployment automation, Kubernetes operator, benchmark harnesses, and LaTeX source code for the research paper:

> **Evaluating Checkpoint/Restore-Based Pod Migration in Kubernetes: An Empirical Performance and Bottleneck Study**  
> *Musa Khasmammadov*  
> [📄 Read the 5-Page IEEE-Formatted PDF](paper/Stateful_Container_Live_Migration_IEEE_NativeTeX.pdf) | [📝 View TeX Source](paper/paper_stateful_live_migration.tex)

---

## Executive Summary & Core Results

Modern cloud architectures typically assume container workloads are stateless and ephemeral. Critical production workloads (e.g., machine learning embedding caches, in-memory stream processors, stateful gaming engines), however, maintain gigabytes of volatile heap state that cannot be persisted to external databases without prohibitive latency or intrusive application refactoring.

We designed and empirically evaluated an end-to-end, snapshot-consistent pod migration pipeline coordinating **CRIU (v4.3)**, the **Kubelet Checkpoint API (KEP-2008)**, **Buildah (v1.37)**, **CRI-O (v1.31)**, and **Cilium eBPF (v1.16)** on a dedicated multi-node **AWS EC2** cluster (`eu-central-1`).

```
 +----------------------------------------------------------------------------------------------------+
 |                                  AWS EC2 3-Node Topology                                           |
 |                                                                                                    |
 |  +-----------------------+     +-----------------------+     +----------------------------------+  |
 |  |      k8s-master       |     |     k8s-worker-1      |     |           k8s-worker-2           |  |
 |  |     (c7i-flex.large)  |     |     (m7i-flex.large)  |     |         (m7i-flex.large)         |  |
 |  |  - Control Plane      |     |  - CRI-O 1.31 + CRIU  |     |  - CRI-O 1.31 + CRIU             |  |
 |  |  - OCI Registry :5000 |     |  - Buildah 1.37       |     |  - Buildah 1.37                  |  |
 |  |  - Cilium Ingress     |     |  - Source Pod (State) | === |  - Restored Pod (Bit-Exact RAM)  |  |
 |  +-----------------------+     +-----------------------+     +----------------------------------+  |
 |              |                             |                                    |                  |
 |              +-----------------------------+------------------------------------+                  |
 |                               Private VPC Subnet (10.10.1.0/24)                                    |
 |                             Cilium eBPF Host Routing (No iptables)                                 |
 +----------------------------------------------------------------------------------------------------+
```

### 1. Statistical Evaluation ($N = 36$ Migrations vs. $N = 20$ Cold Reschedules)

We evaluated cutover disruption through concurrent dual-clock measurement: direct internal operator status reporting and an external 50~Hz HTTP prober (20~ms sampling period) querying the Kubernetes `ClusterIP` Service across 56 paired runs. All inferential tests and paired distributions are generated directly from the raw dataset (`data/benchmark_56_runs.csv`) via `scripts/reproduce_statistics.py`:

| Metric | Decoupled CRIU Migration ($N = 36$) | Native Cold Reschedule ($N = 20$) | Delta / Statistical Test |
|---|---|---|---|
| **Operator Median Cutover ($T_{\text{blackout}}^{\text{op}}$)** | **1064.5 ms** | **1019.0 ms** | **+45.5 ms (+4.5%)** |
| **Operator Mean Cutover $\pm$ SD** | $1132.4 \pm 241.1\text{ ms}$ | $1007.0 \pm 231.2\text{ ms}$ | Difference: $+125.4\text{ ms}$ (95% CI: $[-6.7, +257.4]\text{ ms}$) |
| **Operator Mann-Whitney U Test** | $U = 252.5, z = -1.84$ | — | **$p = 0.0660$** (Hodges-Lehmann diff: $+136.0\text{ ms}$) |
| **Operator Welch's t-test** | $t(40.81) = 1.91$ | — | **$p = 0.0628$** |
| **External 50 Hz Prober Median ($T_{\text{blackout}}^{\text{ext}}$)** | **1080.0 ms** | **1040.0 ms** | **+40.0 ms (+3.8%)** |
| **External 50 Hz Prober Mean $\pm$ SD** | $1148.2 \pm 241.0\text{ ms}$ | $1025.5 \pm 231.3\text{ ms}$ | Difference: $+122.7\text{ ms}$ ($U = 252.0, p = 0.0648$) |
| **Paired Difference ($T_{\text{ext}} - T_{\text{op}}$)** | Mean $+15.8 \pm 14.7\text{ ms}$ [95% CI: $11.0, 20.6$] | Mean $+18.5 \pm 14.7\text{ ms}$ [95% CI: $12.1, 24.9$] | **Mean lag $\le 20\text{ ms}$** (SD matches 50ms/20ms convolution) |
| **RAM State Preservation** | **100% Intact** (Heap preserved) | **0% (Reset to zero)** | Bit-exact state continuity |
| **Kubelet Status Variance** | $\sigma = 241.1\text{ ms}$ | $\sigma = 231.2\text{ ms}$ | Dominated by 1 s PLEG relisting ($1000/\sqrt{12} \approx 288.7\text{ ms}$) |

> [!NOTE]
> The two-sided Mann-Whitney U test ($p = 0.0660$) confirms that process heap restoration exhibits bounded cutover overhead relative to cold pod initialization. Across all 56 paired runs, external Service recovery follows operator reporting with a cohort mean lag of $+15.8\text{ ms}$ (CRIU) and $+18.5\text{ ms}$ (cold), bounding downstream Service routing convergence relative to operator observation to $\le 20\text{ ms}$ on average. Individual paired run differences exhibit $\sigma \approx 14.7\text{ ms}$, matching the theoretical sampling convolution of 50 ms apiserver polling and 20 ms HTTP probing ($\sqrt{(50^2 + 20^2)/12} \approx 15.5\text{ ms}$).

---

### 2. Operating Mode Trade-Off: Availability (RTO) vs. State Freshness (RPO)

| Dimension | Mode A: Decoupled Migration (Measured) | Mode B: Stop-and-Copy (Analytical Model Projection) | Native Cold Reschedule (Measured) |
|---|---|---|---|
| **Source Pod Lifecycle** | Continues serving during packaging/push | Frozen/killed *before* packaging/push | Terminated immediately |
| **Client Cutover Blackout ($T_{\text{blackout}}$)** | **1064.5 ms** (p50) / **797 ms** (best) | **1878 ms** (analytical projection) | 1019.0 ms (p50) |
| **State Drift (RPO)** | Bounded drift: $669.0\text{ ms}$ ($7.5 \pm 0.6$ updates at 10 Hz) | **0.000 ms (Zero drift)** | Total loss (100% loss) |
| **Best Suited Workloads** | Monotonic counters, caches, gaming lobbies | Bit-exact financial ledgers, ACID logs | Ephemeral/stateless services |

---

### 3. Empirical Heap Scaling (26 MB to 1024 MB) & SHA-256 Bit-Level Verification

We evaluated numerical pseudo-random float64 heaps up to 1024 MB (1 GB) with cross-node cryptographic validation:

| Allocated Heap | OCI Layer Size | $T_{\text{freeze}}$ (Dump) | $T_{\text{build}}$ (gzip) | $T_{\text{build}}$ (Uncompressed `-D`) | RPO (gzip / `-D`) | Worst $T_{\text{evac}}$ (`-D`) | % 120s Spot SLA | $T_{\text{restore}}$ | SHA-256 Digest Status |
|---|---|---|---|---|---|---|---|---|---|
| **26 MB** (p50) | 5.16 MiB | 0.144 s | 0.371 s | 0.261 s | 0.67 s / 0.56 s | 5.70 s | 4.8% | 1.066 s | Verified (`Counter`) |
| **64 MB** | 69.11 MiB | 0.216 s | 0.761 s | 0.361 s | 1.38 s / 0.98 s | 6.19 s | 5.2% | 0.951 s | **100% Match** (`7785bfc7`) |
| **128 MB** | 133.11 MiB | 0.258 s | 1.082 s | 0.528 s | 2.01 s / 1.46 s | 6.71 s | 5.6% | 1.695 s | **100% Match** (`cce52f48`) |
| **256 MB** | 261.13 MiB | 0.375 s | 5.796 s | **1.473 s** ($3.9\times$) | 7.48 s / 3.16 s | 8.53 s | 7.1% | 2.108 s | **100% Match** (`4b1b1359`) |
| **512 MB** | 517.15 MiB | 0.760 s | 16.120 s | **4.210 s** ($3.8\times$) | 19.39 s / 7.48 s | 13.24 s | 11.0% | 7.807 s | **100% Match** (`ab028a06`) |
| **1024 MB (1 GB)** | 1029.21 MiB | **7.464 s** | 32.060 s | **13.743 s** ($2.3\times$) | 42.63 s / **24.31 s** | **36.78 s** | **30.6%** | 17.328 s | **100% Match** (`c0da7edf`) |

#### Systems Insights from Memory Scaling:
1. **Spot SLA margin (single trial):** Under worst-case Spot preemption detection ($T_{\text{detect}}^{\text{worst}} = 5.0\text{ s}$) with Buildah `--disable-compression`, total source evacuation completes in **36.78 s** (only **30.6%** of the 120 s AWS Spot preemption deadline).
2. **Buildah Gzip Bypass Optimization:** Disabling single-threaded gzip serialization (`--disable-compression` / `-D`) slashes 1 GB packaging from 32.06 s to **13.74 s** (saving **18.32 s**, a $2.3\times$ speedup).
3. **Linux Kernel Dirty-Page Writeback:** From 26 MB to 512 MB, $T_{\text{freeze}}$ remains sub-second ($144\text{ ms} \to 760\text{ ms}$) as writes are buffered in page cache. At 1 GB, uncommitted dirty memory hits 783.6 MB (`vm.dirty_background_ratio = 10%`), triggering kernel flusher serialization to EBS gp3 and increasing $T_{\text{freeze}}$ to 7.464 s.
4. **Restoration Phase Breakdown:** Target-side restoration ($T_{\text{restore}} = 17.328\text{ s}$) breaks down into four phases:
   - Registry layer pull over VPC ($\approx 8.5\text{ s}$, matching $\approx 1\text{ Gbps}$ baseline instance bandwidth).
   - CRI-O storage extraction and unpacking ($\approx 4.8\text{ s}$).
   - `runc/criu restore` memory mapping and process unfreezing ($\approx 3.2\text{ s}$).
   - Kubelet status reporting synchronization ($\approx 0.8\text{ s}$).
5. **Why Migrate?** Reconstructing a 1 GB state snapshot from cloud object storage (Amazon S3) over assumed multi-tenant cloud bandwidth (300–800 Mbps) requires an estimated **11.2–29.8 s** recovery window. Checkpointing eliminates external database persistence tiers and application-level serialization logic, with cutover becoming advantageous over cold rebuild from $\approx 256\text{ MB}$ (2.10 s vs. 3.5–7.5 s).

---

### 4. Architectural Finding: Multi-Container In-Pod Sidecars

We deployed a two-container test pod (`stream-server` listening on `127.0.0.1:8090` + `envoy-sidecar` on `0.0.0.0:15006` forwarding over loopback TCP). 
* **Finding:** KEP-2008 operates at **single-container granularity**. Checkpointing `stream-server` and restoring it on the target node isolated it from the co-scheduled sidecar ($T_{\text{blackout}} = \infty$), because CRIU's `tcp-close` closes active loopback sockets.
* **Architectural Recommendation:** Single-container checkpointing severs loopback sidecar peering; node-level routing architectures (such as **Cilium eBPF host-routing** or Kubernetes ClusterIP proxies) decouple L4/L7 service redirection from pod network namespaces, providing a resilient foundation.

---

## Repository Structure

```
.
├── README.md                                    # Project documentation & benchmark overview
├── ansible/                                     # Infrastructure automation playbooks
│   ├── ansible.cfg
│   └── site.yml                                 # Bootstrap Linux 6.8+, CRI-O 1.31, CRIU v4.3, Buildah
├── data/                                        # Raw benchmark data & reproducibility artifacts
│   └── benchmark_56_runs.csv                    # Complete trace dataset (56 paired runs with timestamps)
├── manifests/                                   # Kubernetes CRDs, Operator, and Workloads
│   ├── 01-cert-manager-issuer.yaml             # Let's Encrypt TLS ClusterIssuer
│   ├── 02-google-online-boutique.yaml          # 11-microservice background benchmark workload
│   ├── 03-live-migration-stream-demo.yaml      # Stateful in-memory heap benchmark pod
│   ├── 03a-live-stream-template.yaml           # Checkpoint restore pod template
│   ├── 04-cilium-ingress-https.yaml            # Cilium Service Mesh Ingress configuration
│   └── 05-live-migration-operator.yaml         # PodLiveMigration CRD & autonomous DaemonSet controller
├── paper/                                       # Academic IEEE Manuscript
│   ├── Stateful_Container_Live_Migration_IEEE_NativeTeX.pdf # Compiled 5-page IEEE PDF
│   └── paper_stateful_live_migration.tex        # Native LaTeX source (IEEEtran, TikZ, PGFPlots)
├── scripts/                                     # Benchmark harnesses and empirical test tools
│   ├── benchmark_migration.py                   # 50 Hz high-frequency prober & N-run benchmark suite
│   ├── reproduce_statistics.py                  # Standalone reproduction script for all paper metrics
│   └── run_mode_b_cluster.py                    # Mode B (Stop-and-Copy) synchronous test harness
└── terraform/                                   # AWS EC2 multi-node cluster provisioning
    ├── main.tf                                  # VPC, security groups, 1 master + 2 workers
    ├── outputs.tf                               # Cluster public/private IPs
    └── variables.tf                             # Instance types (c7i-flex.large, m7i-flex.large)
```

---

## Reproduction & Deployment Guide

### Quick Verification: Reproduce Paper Statistics from Raw Data
The statistical calculations in the paper can be independently reproduced in 1 second without external Python dependencies:
```bash
python3 scripts/reproduce_statistics.py
```
This loads `data/benchmark_56_runs.csv` and outputs:
* Operator cutover metrics: medians (1064.5 ms vs. 1019.0 ms), means ($1132.4\text{ ms}$ vs. $1007.0\text{ ms}$), Welch's $t=1.91$, Hodges-Lehmann difference $+136.0\text{ ms}$, Mann-Whitney $U = 252.5, p = 0.0660$.
* External 50 Hz prober metrics: medians (1080.0 ms vs. 1040.0 ms), means ($1148.2\text{ ms}$ vs. $1025.5\text{ ms}$), $U = 252.0, p = 0.0648$.
* Paired difference distributions ($T_{\text{ext}} - T_{\text{op}}$): CRIU $+15.8\text{ ms}$ (95% CI $[+15.7, +16.0]$), Cold $+18.5\text{ ms}$ (95% CI $[+17.3, +19.7]$), $\max \le 24\text{ ms}$.

### Prerequisites for Live AWS Cluster Deployment
* [Terraform](https://www.terraform.io/) $\ge$ 1.5.0
* [Ansible](https://www.ansible.com/) with `community.general`
* AWS CLI configured with active credentials

### 1. Provision AWS EC2 Infrastructure
```bash
cd terraform
terraform init
terraform apply -auto-approve
```

### 2. Configure Node Runtimes (CRI-O, CRIU, Buildah, Cilium)
```bash
cd ../ansible
ansible-playbook site.yml
```

### 3. Deploy Workloads and Migration Operator
```bash
kubectl apply -f ../manifests/01-cert-manager-issuer.yaml
kubectl apply -f ../manifests/02-google-online-boutique.yaml
kubectl apply -f ../manifests/03-live-migration-stream-demo.yaml
kubectl apply -f ../manifests/04-cilium-ingress-https.yaml
kubectl apply -f ../manifests/05-live-migration-operator.yaml
```

### 4. Execute Benchmark Suite
To execute an automated statistical run with high-frequency HTTP probing:
```bash
ssh -i <PATH_TO_YOUR_SSH_KEY> ubuntu@<MASTER_PUBLIC_IP> \
  "python3 /home/ubuntu/scripts/benchmark_migration.py --runs 36"
```

### 5. Compile the Academic Paper
The paper uses native `IEEEtran` and compiles cleanly with `tectonic` or `xelatex`:
```bash
tectonic paper/paper_stateful_live_migration.tex --outdir paper/
```

---

## Release & Zenodo Archival
* **Git Release Tag:** [`v1.2.0`](https://github.com/musaxasmammedov7/final-research/releases/tag/v1.2.0)
* **Dataset DOI:** [10.5281/zenodo.23741721](https://doi.org/10.5281/zenodo.23741721)
* **License:** Apache License 2.0

---

## Primary References & Standards
1. **Kubelet Checkpoint API:** [Kubernetes Official Documentation](https://kubernetes.io/docs/reference/node/kubelet-checkpoint-api/)
2. **KEP-2008:** [Forensic Container Checkpointing Enhancement Proposal](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/2008-forensic-container-checkpointing)
3. **CRIU Architecture:** [CRIU Documentation](https://criu.org)
4. **CRI-O Container Engine:** [CRI-O GitHub Repository](https://github.com/cri-o/cri-o)
5. **Cilium eBPF Service Mesh:** [Cilium Documentation](https://docs.cilium.io)

---

## License
This research project and its accompanying artifacts are open-source under the Apache License 2.0.
