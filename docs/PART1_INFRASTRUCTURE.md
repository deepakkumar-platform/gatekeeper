# Part 1 — Infrastructure & GPU VM: Complete Setup Reference

This is the first of three documents covering what we built. It walks every layer below the application — AWS networking, the GPU instance, the OS-level GPU stack, and the systemd service that hosts the gateway. Each section explains *why* the decision was made, not just *what* was done, because in six months you'll need the *why* to debug things or to do this again on a different cloud.

**What this document covers:**
- The full architecture, end to end, with a path-of-a-request diagram
- AWS networking: VPC, subnets, routing, security groups, S3 endpoint
- The Application Load Balancer and its target group
- The g5.2xlarge GPU instance: hardware, storage, AMI
- The GPU software stack: NVIDIA driver → CUDA → cuDNN
- Ollama and the qwen3.5:9b model
- The gatekeeper user, directory layout, systemd unit, permissions
- File and path reference (table)
- Disaster recovery: rebuild from zero in ~30 minutes
- Common operational tasks
- Cost model

**Coming in later parts:**
- Part 2 — the gateway API, its features (per-app keys, rate limit, audit, rotation, hot reload), endpoints, and why these patterns are cloud-independent
- Part 3 — the code itself, file by file, function by function

---

## 1. Architecture overview

```
+------------------+
|   Dev team app   |  Azure, browser, anywhere
|    (Azure)       |
+--------+---------+
         |  HTTPS or HTTP   Authorization: Bearer sk-gatekeeper-...
         v
+------------------+                    AWS ap-south-1
|     Route 53     |  *(once domain is added; today: direct ALB DNS)*
+--------+---------+
         |
         v
+------------------+
|   Application    |  gatekeeper-alb (internet-facing)
|   Load Balancer  |  Listener: HTTP :80  → target group
+--------+---------+  in TWO AZs (1a + 1b) — required by ALB
         |
         v
+------------------+
|   Target group   |  gatekeeper-gw-tg
|   HTTP :8080     |  health check GET /healthz expects 200
+--------+---------+
         |
         |  Security group rule: only ALB SG can reach :8080
         v
+------------------+                  EC2 g5.2xlarge
|   Security       |  gatekeeper-public-snet
|   Group on g5    |  + SSH :22 from your /32 only
+--------+---------+
         |
         v
+--------------------------------------------------+
| g5.2xlarge   ip-10-0-1-10 (private)           |
|              203.0.113.10 (EIP, public)         |
|                                                  |
|  +--------------------------------------------+  |
|  | systemd unit: gatekeeper.service      |  |
|  |   User=gatekeeper                             |  |
|  |   ExecStart=uvicorn app.main:app :8080     |  |
|  +-----------------+--------------------------+  |
|                    |                             |
|                    v                             |
|  +-----------------+--------------------------+  |
|  | uvicorn (FastAPI on :8080)                 |  |
|  |   reads /etc/gatekeeper/keys.yaml             |  |
|  |   writes /var/log/gatekeeper/requests.jsonl   |  |
|  |   proxies to localhost backends            |  |
|  +-----------------+--------------------------+  |
|                    |                             |
|                    v  HTTP localhost only        |
|  +-----------------+--------------------------+  |
|  | ollama on 127.0.0.1:11434                  |  |
|  |   loads qwen3.5:9b into A10G VRAM          |  |
|  |   24 GB GPU, model uses ~6.7 GB            |  |
|  +--------------------------------------------+  |
|                                                  |
|  + cuDNN + CUDA 13 + NVIDIA driver 580 + GPU     |
+--------------------------------------------------+
```

**Why so many layers?** Each one has a single job:

- **ALB** terminates the public connection so apps never speak directly to your EC2. Lets you swap the EC2 underneath without changing the dev team's URL.
- **Target group + health check** decides whether the EC2 is alive. If `/healthz` stops returning 200, the ALB takes the target out of rotation automatically.
- **Security groups** enforce that only the ALB can reach `:8080` — even if someone discovers the EC2's public IP, they can't bypass the ALB.
- **systemd** keeps the gateway running across reboots and auto-restarts on crash.
- **uvicorn + FastAPI** is the gateway: auth, rate limit, audit logging, request forwarding.
- **Ollama** is the LLM runtime, bound to localhost only — the gateway is the only thing that can reach it.

The shape — *gateway in front of a backend, both on the same host, only the gateway public-facing* — is the same shape OpenAI, Anthropic, and every API provider uses. It's cloud-independent. If you redeployed on Azure tomorrow, you'd swap ALB for Azure Application Gateway, EC2 for an Azure VM, and the rest stays identical.

---

## 2. AWS networking

### 2.1 The VPC

```
Name:        gatekeeper-vpc
CIDR:        10.3.192.0/20
Total IPs:   4096   (range: 10.3.192.0 – 10.3.207.255)
IPv6:        none
Region:      ap-south-1 (Mumbai)
```

**Why /20 and not the default /16?** Default VPC is `172.31.0.0/16` (65k IPs). You allocated a /20 because the organization's platform team carves up address space and assigns slices to projects. A /20 (4096 IPs) is overkill for one POC but leaves room for ~30 future workloads in the same VPC.

**Why `10.3.192.0/20` specifically?** The third octet `192` is a multiple of 16, which is what /20 requires (each /20 spans 16 consecutive third-octet values). `10.3.179.0/20` would have been rejected by AWS because 179 isn't a /20 boundary.

> **CIDR-boundary cheat sheet** — A network mask `/N` aligns to `2^(32-N)` addresses. So /20 aligns to 4096, which means the third octet must be a multiple of 16. /22 aligns to 1024 (third octet multiple of 4). /24 aligns to 256 (third octet anything 0-255). When AWS says "invalid CIDR," it's almost always a boundary problem.

### 2.2 Subnets

```
gatekeeper-subnet-public1-ap-south-1a   10.3.192.0/22    1024 IPs   ap-south-1a   PUBLIC
gatekeeper-subnet-private1-ap-south-1a  10.3.196.0/22    1024 IPs   ap-south-1a   PRIVATE
gatekeeper-subnet-public2-ap-south-1b   10.3.200.0/23     510 IPs   ap-south-1b   PUBLIC
```

**Why three subnets and not two?**

- The g5 lives in `public1-ap-south-1a`. It needs internet access to pull packages and serve traffic, so it's in a *public* subnet.
- `private1-ap-south-1a` is reserved for stateful services (RDS, ElastiCache, Cassandra, etc.) when they get added later. Empty today, costs nothing, here so the VPC has the shape of a real production network.
- `public2-ap-south-1b` was added later because the ALB **requires subnets in at least two AZs** — even though only one of them actually hosts a target. AWS gives the ALB ENIs (Elastic Network Interfaces) in both subnets so it can survive an AZ failure.

**Why /22 for first two but /23 for the 1b subnet?** Mostly because /22s in slots 192 and 196 used the first half of the VPC, and the 1b subnet was added later as a smaller carve-out of the remaining space. /23 (510 usable IPs) is still massively oversized for an ALB but matches the corporate "give it room" style. The remaining free range inside the VPC (`10.3.202.0/23` through `10.3.207.255`) is available for future expansion.

**Public vs private — what's the actual difference?** Both are real subnets with real IP ranges. The only thing that makes one "public" is its route table: a public subnet has a route `0.0.0.0/0 → IGW`, sending internet-bound traffic through the Internet Gateway. A private subnet's default route either doesn't exist (no outbound internet) or goes to a NAT Gateway (outbound only, no inbound from internet).

### 2.3 Internet Gateway and route tables

```
gatekeeper-igw                          attached to gatekeeper-vpc

gatekeeper-rtb-public                   2 subnet associations (both public subnets)
  Routes:
    10.3.192.0/20  → local           (intra-VPC, automatic)
    0.0.0.0/0      → gatekeeper-igw     (everything else to the internet)

gatekeeper-rtb-private1-ap-south-1a     1 subnet association (private subnet)
  Routes:
    10.3.192.0/20  → local
    pl-78a54011    → gatekeeper-vpce-s3 (S3 gateway endpoint, see below)
  (no 0.0.0.0/0 route — that's what makes the subnet "private")
```

**The IGW does only two things:**
1. Lets instances with public IPs receive inbound traffic from the internet.
2. Performs NAT for outbound traffic from public-subnet instances.

It doesn't cost anything. There's literally no charge for an IGW. The cost in AWS comes from *data transfer*, not from the IGW itself.

**Why NO NAT Gateway?** Because the g5 sits in the public subnet with an EIP, it gets internet egress through the IGW directly — no NAT needed. A NAT Gateway costs ~$32/month plus data charges, and adds nothing for this design. People run NAT Gateways when their workloads live in *private* subnets but still need internet egress (e.g. to pull container images). You don't have that.

### 2.4 S3 Gateway Endpoint

```
Name:        gatekeeper-vpce-s3
Type:        Gateway endpoint
Service:     com.amazonaws.ap-south-1.s3
Attached to: gatekeeper-rtb-private1-ap-south-1a (and could attach to public too)
```

**Why have this even though you have an IGW?** Two reasons:

1. **No data-transfer charges.** Without the endpoint, S3 traffic from your VPC goes out via the IGW and counts as internet egress (which is metered after the free tier — ~$0.09/GB once you're past it). With the endpoint, S3 traffic stays on AWS's internal backbone and is free.
2. **Faster, more reliable.** Internal AWS routes have lower latency and don't depend on internet routing.

S3 Gateway endpoints are **free** — no hourly charge, no data charge. Always create one when your workload touches S3. The drone-footage workload you've described would hit this hard.

### 2.5 Security groups — the most important concept here

You created two security groups. Understanding why they look the way they do is critical.

**SG #1: `gatekeeper-public-snet` (sg-0bbbbbbbbbbbbbbbb)** — attached to the g5 EC2

```
Inbound:
  TCP 8080  source: sg-0aaaaaaaaaaaaaaaa (gatekeeper-alb-sg)   ← gateway from ALB only
  TCP 22    source: 175.107.142.216/32                       ← your office /32
  TCP 22    source: 13.234.72.80/32                          ← your bastion/EIP /32
Outbound:
  ALL       0.0.0.0/0
```

**SG #2: `gatekeeper-alb-sg` (sg-0aaaaaaaaaaaaaaaa)** — attached to the ALB

```
Inbound:
  TCP 443   0.0.0.0/0                                        ← HTTPS from anywhere (when on)
  TCP 80    0.0.0.0/0                                        ← HTTP from anywhere (POC)
Outbound:
  ALL       0.0.0.0/0
```

**The crucial pattern:** SG #1's inbound `:8080` rule names *SG #2 as its source*. This is called an **SG-to-SG reference**.

```
                       (only this SG can reach 8080)
   internet ── ALB ─────── gatekeeper-alb-sg ──────────── gatekeeper-public-snet
              (any IP)                                     (g5 EC2)
```

**Why this matters:**

- The ALB has multiple internal IPs spread across AZs, and AWS rotates them. If you wrote inbound rules like "allow `:8080` from specific ALB IPs," those would break on every IP rotation.
- The SG reference is **stable**. SG IDs don't change; the rule "allow `:8080` from SG `gatekeeper-alb-sg`" continues working forever, regardless of ALB IP changes, scaling events, etc.
- Even if someone discovers the g5's public IP (`203.0.113.10`), they can't reach `:8080` directly because *they aren't a member of `gatekeeper-alb-sg`*. They'd hit a TCP refusal. Defense in depth.

This pattern works in any cloud. Azure's NSGs have application security groups; GCP has tags-as-sources. The principle is the same: reference identities, not IPs.

> **The single most common rookie mistake** is leaving `:8080` open to `0.0.0.0/0` "for testing." Don't. Even a 30-minute window of this opens you to internet scanners. Use SSM Session Manager + port forwarding if you need to hit the gateway directly during debugging.

### 2.6 What does NOT exist (and why)

You may notice things absent from this VPC that exist in fancier architectures:

- **No NAT Gateway** — see above; not needed in this design.
- **No VPC peering, no Transit Gateway** — you're not connecting to other VPCs.
- **No VPC Flow Logs** — useful for forensic debugging, but adds CloudWatch costs. Add when you have real traffic and incidents to investigate.
- **No PrivateLink endpoints** beyond S3 — costs $7-$20/month each. Add when you start hitting other AWS services heavily (Secrets Manager, etc.) and want to keep that traffic off the internet.
- **No IPv6** — keeps the design simpler. Re-enable when you have a reason.
- **No second private subnet (1b)** — only needed for HA database tiers. Add when RDS Multi-AZ enters the picture.

---

## 3. The Application Load Balancer

### 3.1 ALB anatomy

```
Name:                gatekeeper-alb
DNS name:            your-alb-0000000000.ap-south-1.elb.amazonaws.com
Scheme:              internet-facing
IP type:             IPv4
VPC:                 gatekeeper-vpc
Availability zones:  ap-south-1a (uses gatekeeper-subnet-public1-ap-south-1a)
                     ap-south-1b (uses gatekeeper-subnet-public2-ap-south-1b)
Security groups:     gatekeeper-alb-sg
Listeners:
  HTTP   :80  → forward → gatekeeper-gw-tg
  HTTPS  :443 → (not configured yet, added once domain + ACM cert ready)
```

The ALB is the only thing outside the VPC that can reach in. Everything else is locked behind it.

### 3.2 Target group

```
Name:                gatekeeper-gw-tg
Target type:         Instances
Protocol/port:       HTTP : 8080
VPC:                 gatekeeper-vpc
Protocol version:    HTTP1
Targets registered:  the g5 EC2 instance, on port 8080
Health check:
  Protocol:          HTTP
  Path:              /healthz
  Port:              traffic port (8080)
  Healthy threshold: 2
  Unhealthy threshold: 3
  Timeout:           5s
  Interval:          30s
  Success codes:     200
```

**How health checks actually work:** every 30 seconds, the ALB sends `GET /healthz` to the registered instance on `:8080`. If it returns 200, the target is "healthy" and gets traffic. If it returns anything else (or times out) 3 consecutive times, the ALB marks it "unhealthy" and stops routing to it. Once it returns 200 twice in a row again, it's "healthy" again.

**Why `/healthz`?** It's an unauthenticated endpoint on the gateway that returns 200 with backend status JSON. The ALB just needs the 200 — it doesn't read the body.

**Why HTTP not HTTPS between ALB and gateway?** Standard AWS pattern: terminate TLS at the ALB, plain HTTP on the back end. The traffic between ALB and EC2 stays inside the VPC on AWS's internal network — already isolated. Saves CPU on the EC2 and avoids a self-signed certificate dance. For genuinely high-security needs you'd add mTLS between ALB and target, but for this workload it's unnecessary.

### 3.3 What the dev team sees

Today (HTTP): `http://your-alb-0000000000.ap-south-1.elb.amazonaws.com`

Tomorrow (HTTPS, when domain added): `https://gatekeeper.<your-domain>`

They never know about the EC2, its IP, the security group, the systemd unit, or Ollama. From their side, it's a URL that returns JSON.

---

## 4. The GPU instance

### 4.1 Hardware

```
Instance type:   g5.2xlarge
GPU:             1x NVIDIA A10G, 24 GB VRAM
vCPU:            8
RAM:             32 GB
Network:         up to 10 Gbps
Public IP:       203.0.113.10 (Elastic IP, stays attached across stop/start)
Private IP:      10.0.1.10
Hostname:        ip-10-0-1-10
```

**Why g5.2xlarge specifically:**
- The A10G is a 2020 datacenter GPU, well-suited to LLM inference. 24 GB VRAM holds qwen3.5:9b (uses ~6.7 GB) with massive headroom for larger models.
- 8 vCPU and 32 GB RAM means you can run the gateway, Ollama, *and* the future MPPT classifier all on one box without contention.
- ~$1.45/hr on-demand in ap-south-1. ~₹125/hr. About ₹3000/day if left running 24x7.

**g5.xlarge vs g5.2xlarge:** xlarge has only 4 vCPU and 16 GB RAM, which becomes the bottleneck (not the GPU) when you're also doing video preprocessing for drone footage. 2xlarge is the right floor for this workload.

### 4.2 Storage

```
Root volume:   100 GB gp3, encrypted
Data volume:   500 GB gp3, encrypted   (separate volume, mounted somewhere like /data)
```

Two volumes deliberately:
- **Root** holds the OS, drivers, Ollama, models. If you ever want to swap the OS or do a clean rebuild, you don't lose data.
- **Data** holds training datasets, model artifacts, logs you want to preserve. Survives instance termination if you detach it first (or you can snapshot it for backup).

gp3 not gp2 — same price, baseline 3000 IOPS guaranteed (gp2 throttles based on volume size).

### 4.3 AMI choice — and the path we took

You went with vanilla **Ubuntu 24.04** rather than the Deep Learning AMI. That meant installing drivers, CUDA, cuDNN, and the Python stack manually. It worked, but the Deep Learning AMI bundles all of that pre-tested.

For future g5 builds, my recommendation order is:

1. **First choice:** Deep Learning AMI (DLAMI) on Ubuntu 22.04 — `Deep Learning OSS Nvidia Driver AMI GPU PyTorch 2.x`. Skip ~1 hour of setup, guaranteed compatibility.
2. **If you have to use 24.04:** vanilla Ubuntu 24.04 + manual driver install (what you did). Works, but requires the steps in section 5.

You learned more by doing it manually, so it wasn't wasted time. But for the next build, DLAMI.

---

## 5. The GPU software stack

### 5.1 The dependency pyramid

```
                  +---------------------------+
                  |    PyTorch / Ollama       |  ← what your code uses
                  +---------------------------+
                                |
                                v
                  +---------------------------+
                  |          cuDNN            |  ← optimized DNN ops
                  +---------------------------+
                                |
                                v
                  +---------------------------+
                  |    CUDA Toolkit (13.0)    |  ← runtime + libraries
                  +---------------------------+
                                |
                                v
                  +---------------------------+
                  |  NVIDIA driver (580)      |  ← kernel module
                  +---------------------------+
                                |
                                v
                  +---------------------------+
                  |       A10G GPU            |  ← hardware
                  +---------------------------+
```

**Two rules to remember:**

1. The driver must support a CUDA version **≥** the toolkit you install. (Drivers are forward-compatible: a driver supporting CUDA 13 can run apps built for CUDA 11.)
2. PyTorch/Ollama/etc. need to be compiled against a CUDA version **≤** what your driver supports.

`nvidia-smi` shows the *driver's maximum supported CUDA version* in the top-right — not what's actually installed. So when you saw `CUDA Version: 13.0` in `nvidia-smi`, that meant "this driver can run apps built up to CUDA 13," not "CUDA 13 toolkit is installed."

### 5.2 NVIDIA driver

```
Installed package:  nvidia-driver-580-server
Version:            580.159.03
```

**The driver is a kernel module.** That's why it must match the kernel version. If you ever run `sudo apt upgrade` and a new kernel lands, you may need to rebuild the driver module via DKMS (the package handles this automatically if `nvidia-dkms-580-server` is installed).

**Verifying:**
```bash
nvidia-smi                  # the whole status display
lsmod | grep nvidia         # shows the loaded kernel modules
modinfo nvidia              # driver version, supported kernels
```

**The single most common driver failure mode** is `nvidia-smi` returning `NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver`. Usually means the kernel was updated but the driver module wasn't rebuilt. Fix:

```bash
sudo apt install --reinstall nvidia-driver-580-server
sudo reboot
```

### 5.3 CUDA Toolkit

CUDA is a *runtime + libraries + headers + compiler (`nvcc`)*. It's the SDK that lets software actually use the GPU for general compute.

For your install, Ollama bundles enough CUDA itself (its binary is linked against the libraries it needs), so you didn't have to install the full toolkit. If you were going to compile PyTorch/CUDA code yourself, you would.

To check what Ollama is using:
```bash
ldd $(which ollama) | grep cuda    # libraries it links against
```

### 5.4 cuDNN

cuDNN ships pre-optimized implementations of operations neural networks use a lot: convolutions, LSTM cells, attention, batch normalization, etc. Without it, those ops fall back to slower generic implementations. Ollama uses it under the hood; you didn't install it explicitly because Ollama bundles what it needs.

If/when you set up PyTorch directly for the MPPT classifier, you'll install it then.

### 5.5 Verification — what `nvidia-smi` actually shows you

```
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 580.159.03             Driver Version: 580.159.03     CUDA Version: 13.0     |
+-----------------------------------------+------------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
|=========================================+========================+======================|
|   0  NVIDIA A10G                    Off |   00000000:00:1E.0 Off |                    0 |
|  0%   39C    P0             60W /  300W |    6709MiB /  23028MiB |      0%      Default |
+-----------------------------------------+------------------------+----------------------+
|    0   N/A  N/A            7673      C   ...local/lib/ollama/llama-server       6700MiB |
+-----------------------------------------+------------------------+----------------------+
```

What to read:
- **Driver Version 580.159.03** — confirms the driver loaded successfully.
- **CUDA Version: 13.0** — the *maximum* version this driver supports. (Not what's installed.)
- **A10G** — the GPU. Matches what AWS sold you.
- **6709 MiB / 23028 MiB** — VRAM used / total. Currently ~6.7 GB used by Ollama loading qwen3.5:9b. ~16 GB free.
- **0% GPU-Util** — idle. During inference this jumps to 70–95%.
- **60W / 300W** — power draw vs maximum. Will rise to ~150–200W under load.
- **39°C** — temperature. A10G throttles around 90°C; you'll never get close on this workload.
- **Processes list** — shows what's using the GPU. `llama-server` is Ollama's actual model-serving binary.

`watch -n 1 nvidia-smi` runs it every second — useful while you're issuing inference requests to watch the GPU work.

---

## 6. Ollama and qwen3.5

### 6.1 What Ollama is

Ollama is a single binary that:
- Hosts an HTTP API for chat and generation
- Manages model downloads (pulls quantized models from a registry)
- Handles loading models into GPU memory
- Bundles its own CUDA libraries (no separate install needed)

It's roughly the LLM equivalent of Docker for models: `ollama pull <model>` is like `docker pull <image>`, `ollama run` is like `docker run`. Behind the scenes it uses **llama.cpp**, a high-performance inference engine.

API endpoints (the ones the gateway uses):
- `POST /api/generate` — single prompt → completion
- `POST /api/chat` — message array → assistant message
- `GET /api/tags` — list installed models

### 6.2 Installation

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

That script:
- Installs `ollama` binary at `/usr/local/bin/ollama`
- Creates a systemd unit at `/etc/systemd/system/ollama.service`
- Creates user `ollama` to run as
- Enables and starts the service

After install:
```bash
systemctl status ollama
ollama --version
```

### 6.3 Pulling the model

```bash
ollama pull qwen3.5:9b
ollama list                              # see what's installed
```

The model is stored at `/usr/share/ollama/.ollama/models/` (or `~/.ollama/models/` if you ran without sudo). qwen3.5:9b is ~6 GB on disk. The first request after pull loads it into VRAM and warms the cache.

### 6.4 Securing it (localhost-only)

By default, Ollama listens on all interfaces (`0.0.0.0:11434`). This is fine for development but unsafe in production — anyone who reaches your EC2 on port 11434 could query the model without authentication, bypass your gateway entirely, bypass your audit log, and rack up GPU usage.

To bind it to localhost only:

```bash
sudo systemctl edit ollama
```

Add:
```
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
ss -tln | grep 11434     # confirm 127.0.0.1:11434, NOT 0.0.0.0:11434
```

After this, only processes on the same host can reach Ollama. Your gateway runs on the same host, so it works fine. Outside callers must go through the gateway, which means they must have a valid API key.

> **If you haven't done this step yet, do it before letting any real dev team integrate.** Your security group blocks port 11434 from the internet, but defense-in-depth says you don't rely on a single layer — bind it to localhost too.

---

## 7. The Gatekeeper gateway service

### 7.1 The `gatekeeper` user

```bash
sudo useradd --system --home /opt/gatekeeper --shell /usr/sbin/nologin gatekeeper
```

A dedicated, non-login system user. Why:

- **Process isolation** — the gateway runs as `gatekeeper`, not `root`. If the gateway is ever compromised, the blast radius is limited to what `gatekeeper` can do (which is very little).
- **File-permission discipline** — the keys file is readable only by `gatekeeper`. Even root has to use `sudo -u gatekeeper` to interact correctly (this is what bit you earlier when you ran the CLI as root and broke the file ownership).
- **Convention** — every production service should run as its own user. Systemd makes this trivial.

`--shell /usr/sbin/nologin` means no one can `su - gatekeeper` to get a shell as this user. The only thing that runs as `gatekeeper` is the systemd unit.

### 7.2 Directory layout

```
/opt/gatekeeper/                       ← code + venv (owned by gatekeeper)
├── app/                            ← FastAPI application
│   ├── __init__.py
│   ├── main.py                     ← routes, middleware, lifespan
│   ├── auth.py                     ← API-key validation
│   ├── rate_limit.py               ← token-bucket limiter
│   ├── proxy.py                    ← backend forwarding (Ollama, classifier)
│   ├── models.py                   ← Pydantic request/response schemas
│   ├── storage.py                  ← keys file + audit log
│   └── config.py                   ← env-var loader
├── scripts/
│   └── gatekeeper-keys                ← admin CLI (installed to /usr/local/bin too)
└── venv/                           ← Python virtual environment
    ├── bin/python3
    ├── bin/uvicorn
    └── lib/python3.12/site-packages/...

/etc/gatekeeper/                       ← configuration (owned by gatekeeper, mode 700)
├── gatekeeper.env                     ← environment file for systemd (600)
└── keys.yaml                       ← per-app keys + metadata (600)

/var/log/gatekeeper/                   ← runtime data (owned by gatekeeper)
└── requests.jsonl                  ← one JSON object per request

/etc/systemd/system/
└── gatekeeper.service         ← systemd unit (root-owned, 644)

/usr/local/bin/
└── gatekeeper-keys                    ← admin CLI shim (root-owned, 755)

/etc/profile.d/
└── gatekeeper.sh                      ← shell helper (sets GATEKEEPER_KEYS_FILE)
```

**The three-bucket pattern** (configuration / code / runtime data) is FHS standard:
- `/opt/gatekeeper/` — the immutable application (code, deps)
- `/etc/gatekeeper/` — configuration (changes rarely, hand-edited if needed)
- `/var/log/gatekeeper/` — runtime-produced data (changes constantly, may rotate)

This split means you can back up `/etc/gatekeeper/` separately, log-rotate `/var/log/gatekeeper/`, and redeploy `/opt/gatekeeper/` without touching the other two.

### 7.3 Python venv and dependencies

```bash
sudo python3 -m venv /opt/gatekeeper/venv
sudo /opt/gatekeeper/venv/bin/pip install -U pip
sudo /opt/gatekeeper/venv/bin/pip install -r /opt/gatekeeper/requirements.txt
```

The venv is isolated from system Python. If Ubuntu pushes a Python security update, your gateway keeps using the venv's Python and packages — unaffected.

Pinned dependencies (`requirements.txt`):
```
fastapi==0.115.5
uvicorn[standard]==0.32.1
httpx==0.27.2
pydantic==2.9.2
pydantic-settings==2.6.1
pyyaml==6.0.2
python-multipart==0.0.12
```

Pinned versions avoid the "it worked last week, now it breaks" surprise.

### 7.4 Permissions matrix

| Path | Owner | Mode | Why |
|---|---|---|---|
| `/opt/gatekeeper/` | `gatekeeper:gatekeeper` | `755` | Read for everyone, write only for gatekeeper |
| `/opt/gatekeeper/venv/` | `gatekeeper:gatekeeper` | `755` | Same |
| `/etc/gatekeeper/` | `gatekeeper:gatekeeper` | `700` | Only gatekeeper can even enter the directory |
| `/etc/gatekeeper/keys.yaml` | `gatekeeper:gatekeeper` | `600` | API key hashes; no one else reads |
| `/etc/gatekeeper/gatekeeper.env` | `gatekeeper:gatekeeper` | `600` | Env file; no secrets today but contains paths |
| `/var/log/gatekeeper/` | `gatekeeper:gatekeeper` | `755` | Logs must be written by gatekeeper |
| `/var/log/gatekeeper/requests.jsonl` | `gatekeeper:gatekeeper` | `644` | Logs readable for inspection |
| `/etc/systemd/system/gatekeeper.service` | `root:root` | `644` | Systemd unit — root-managed |
| `/usr/local/bin/gatekeeper-keys` | `root:root` | `755` | Executable for anyone, modifiable only by root |

**The biggest gotcha** is the keys file. If you `sudo gatekeeper-keys issue ...`, the file gets rewritten as root, and on the next request the gateway (running as `gatekeeper`) gets `PermissionError` reading it → 500 error. That's exactly what bit you earlier. Always:

```bash
sudo -u gatekeeper gatekeeper-keys <command>
```

The alias in `/etc/profile.d/gatekeeper.sh` makes this transparent:
```bash
alias gatekeeper-keys='sudo -u gatekeeper gatekeeper-keys'
```

After that, just typing `gatekeeper-keys list` does the right thing.

### 7.5 The systemd unit

`/etc/systemd/system/gatekeeper.service`:

```ini
[Unit]
Description=Gatekeeper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gatekeeper
Group=gatekeeper
WorkingDirectory=/opt/gatekeeper
EnvironmentFile=/etc/gatekeeper/gatekeeper.env
ExecStart=/opt/gatekeeper/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers
Restart=on-failure
RestartSec=5
LimitNOFILE=65535

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/log/gatekeeper /etc/gatekeeper

[Install]
WantedBy=multi-user.target
```

Going line by line on the interesting parts:

- `After=network-online.target` / `Wants=network-online.target` — wait until the network is up before starting. Otherwise on boot, the gateway might start before the EIP attaches.
- `User=gatekeeper` — process runs as the `gatekeeper` user. Critical.
- `WorkingDirectory=/opt/gatekeeper` — `uvicorn app.main:app` needs `app/` to be importable. Setting cwd to `/opt/gatekeeper` puts `app/` on Python's path.
- `EnvironmentFile=/etc/gatekeeper/gatekeeper.env` — pulls env vars (paths, settings) from the file.
- `ExecStart=...` — actual command. `--host 0.0.0.0` binds on all interfaces. `--proxy-headers` tells uvicorn to honor `X-Forwarded-For` and `X-Forwarded-Proto` from the ALB so the audit log captures the real client IP and protocol.
- `Restart=on-failure` / `RestartSec=5` — if uvicorn crashes, wait 5 seconds, restart. Survives transient errors.

The "hardening" block matters:
- `NoNewPrivileges=yes` — process and its children can't gain extra privileges (no `setuid` escalation).
- `ProtectSystem=strict` — the entire filesystem is read-only to the process, except what's explicitly allowed.
- `ProtectHome=yes` — `/home/*` and `/root` are inaccessible.
- `PrivateTmp=yes` — process gets its own `/tmp` namespace, can't see other processes' tmp files.
- `ReadWritePaths=/var/log/gatekeeper /etc/gatekeeper` — only these paths are writable. Everywhere else is read-only.

That hardening means even if someone compromised the gateway via a code vulnerability, they can't write anywhere except `/var/log/gatekeeper/` and `/etc/gatekeeper/`. Significantly limits what an attacker can do.

### 7.6 The CLI installation

```bash
sudo install -m 0755 /opt/gatekeeper/scripts/gatekeeper-keys /usr/local/bin/gatekeeper-keys
```

`install` is `cp` with explicit modes. The CLI ends up at `/usr/local/bin/gatekeeper-keys` so it's on every user's PATH.

The CLI itself is a small Python script that:
- Reads/writes `/etc/gatekeeper/keys.yaml`
- Generates new keys with `secrets.token_urlsafe(36)` → `sk-gatekeeper-<random>`
- Stores only the SHA-256 hash of each key
- Prints the plaintext exactly once (`issue` / `rotate` operations)

The path of `/etc/gatekeeper/keys.yaml` is read from the `GATEKEEPER_KEYS_FILE` environment variable, set by `/etc/profile.d/gatekeeper.sh`.

### 7.7 Verification — the full bring-up smoke test

After everything is installed:

```bash
# 1. Service is running
sudo systemctl status gatekeeper --no-pager | head -5
# Expect: Active: active (running)

# 2. Process is listening
ss -tln | grep :8080
# Expect: LISTEN ... 0.0.0.0:8080 ...

# 3. Health endpoint responds locally
curl -s http://127.0.0.1:8080/healthz | jq
# Expect: {"status":"ok","backends":{"ollama":"ok",...}}

# 4. Health endpoint responds through the ALB
curl -s http://your-alb-0000000000.ap-south-1.elb.amazonaws.com/healthz | jq
# Same JSON

# 5. CLI works
sudo -u gatekeeper gatekeeper-keys list
# Expect: existing apps (or empty)

# 6. Auth works
sudo -u gatekeeper gatekeeper-keys issue test --rate 60 --notes "smoke test"
# Capture the printed key
curl -H "Authorization: Bearer sk-gatekeeper-..." \
  http://your-alb-0000000000.ap-south-1.elb.amazonaws.com/v1/me
# Expect: {"app":"test","active":true,...}

# 7. Audit log captures
sudo tail -n 1 /var/log/gatekeeper/requests.jsonl | jq

# 8. Cleanup
sudo -u gatekeeper gatekeeper-keys remove test
```

If all 8 pass, the entire stack from kernel module → driver → CUDA → Ollama → systemd → uvicorn → ALB → public internet is working.

---

## 8. File and path reference

Quick lookup for every path/file involved.

| Path | Purpose | Owner | Mode |
|---|---|---|---|
| `/opt/gatekeeper/app/` | FastAPI source | gatekeeper | 755 |
| `/opt/gatekeeper/scripts/gatekeeper-keys` | Admin CLI (source) | gatekeeper | 755 |
| `/opt/gatekeeper/venv/` | Python virtualenv | gatekeeper | 755 |
| `/etc/gatekeeper/gatekeeper.env` | Gateway environment vars | gatekeeper | 600 |
| `/etc/gatekeeper/keys.yaml` | API keys (hashes only) + per-app config | gatekeeper | 600 |
| `/var/log/gatekeeper/requests.jsonl` | One JSON line per request | gatekeeper | 644 |
| `/etc/systemd/system/gatekeeper.service` | systemd unit | root | 644 |
| `/etc/systemd/system/ollama.service.d/override.conf` | Ollama localhost binding | root | 644 |
| `/usr/local/bin/gatekeeper-keys` | CLI shim | root | 755 |
| `/etc/profile.d/gatekeeper.sh` | Shell helpers (sets KEYS_FILE, alias) | root | 644 |
| `/usr/share/ollama/.ollama/models/` | Ollama model store | ollama | varies |
| `/usr/local/bin/ollama` | Ollama binary | root | 755 |

Common commands referencing each:

```bash
# View service status
sudo systemctl status gatekeeper
sudo systemctl status ollama

# View logs (live)
sudo journalctl -u gatekeeper -f
sudo tail -f /var/log/gatekeeper/requests.jsonl | jq

# View logs (last 50)
sudo journalctl -u gatekeeper -n 50

# Restart / stop / start
sudo systemctl restart gatekeeper
sudo systemctl stop gatekeeper
sudo systemctl start gatekeeper

# Edit config (then restart)
sudo $EDITOR /etc/gatekeeper/gatekeeper.env
sudo systemctl restart gatekeeper

# Manage keys (use sudo -u gatekeeper, NOT plain sudo)
sudo -u gatekeeper gatekeeper-keys list
sudo -u gatekeeper gatekeeper-keys issue <app> --rate <n> --notes "..."
sudo -u gatekeeper gatekeeper-keys rotate <app>
sudo -u gatekeeper gatekeeper-keys disable <app>
sudo -u gatekeeper gatekeeper-keys enable <app>
sudo -u gatekeeper gatekeeper-keys remove <app>
```

---

## 9. Disaster recovery — rebuild from zero

You're a DevOps engineer; you know "can't redeploy from scratch" = "doesn't exist." Here's the full procedure.

### 9.1 What's lost if the instance dies

- **Lost permanently** (if not backed up):
  - `/etc/gatekeeper/keys.yaml` — API keys (every app must get new keys)
  - `/var/log/gatekeeper/requests.jsonl` — historical audit log
  - Anything written to instance store

- **Survives**:
  - The VPC, subnets, SGs, ALB, target group, IGW, route tables (these are separate AWS resources)
  - The EIP (stays in the account until released)
  - EBS data volume if marked "delete on terminate = false" and detached cleanly

### 9.2 What to back up

```bash
# Nightly cron on the gateway: snapshot the keys file to S3
0 2 * * * aws s3 cp /etc/gatekeeper/keys.yaml \
  s3://gatekeeper-backups/keys/keys.yaml.$(date +%F) --sse AES256

# Optional: archive the audit log weekly
0 3 * * 0 aws s3 cp /var/log/gatekeeper/requests.jsonl \
  s3://gatekeeper-backups/logs/requests.$(date +%F).jsonl --sse AES256 \
  && truncate -s 0 /var/log/gatekeeper/requests.jsonl
```

Create the bucket once with versioning enabled (`aws s3api put-bucket-versioning --bucket gatekeeper-backups --versioning-configuration Status=Enabled`).

### 9.3 Full rebuild script — runnable end to end

Assuming AWS network resources still exist (VPC, ALB, target group, SG). If those are gone too, you'd run Terraform first; that's a future improvement.

```bash
#!/bin/bash
# bring-up.sh — Gatekeeper gateway from blank Ubuntu 24.04 g5.2xlarge
# Run as ubuntu user, with sudo available.
set -euo pipefail

echo "=== [1/9] System update + base packages ==="
sudo apt update
sudo apt install -y build-essential dkms linux-headers-$(uname -r) \
  gcc make curl wget git unzip jq python3 python3-venv python3-pip

echo "=== [2/9] Blacklist nouveau ==="
sudo tee /etc/modprobe.d/blacklist-nouveau.conf >/dev/null <<'EOF'
blacklist nouveau
options nouveau modeset=0
EOF
sudo update-initramfs -u

echo "=== [3/9] NVIDIA driver — REBOOT REQUIRED AFTER THIS STEP ==="
sudo apt install -y nvidia-driver-580-server
echo ">>> Reboot now, then continue with the next block. <<<"
exit 0
```

After reboot, second half:

```bash
#!/bin/bash
# bring-up-part2.sh — continue after reboot
set -euo pipefail

echo "=== [4/9] Verify driver ==="
nvidia-smi || { echo "Driver not loaded"; exit 1; }

echo "=== [5/9] Install Ollama and pull qwen3.5:9b ==="
curl -fsSL https://ollama.com/install.sh | sh

# Bind ollama to localhost
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama

ollama pull qwen3.5:9b
ollama list

echo "=== [6/9] gatekeeper user + directory layout ==="
sudo useradd --system --home /opt/gatekeeper --shell /usr/sbin/nologin gatekeeper || true
sudo mkdir -p /opt/gatekeeper /etc/gatekeeper /var/log/gatekeeper

echo "=== [7/9] Gateway code + venv ==="
# Assumes gatekeeper-v0.2.0.tar.gz was scp'd to ~/
cd ~ && tar -xzf gatekeeper-v0.2.0.tar.gz
cd gatekeeper

sudo cp -r app scripts requirements.txt /opt/gatekeeper/

sudo python3 -m venv /opt/gatekeeper/venv
sudo /opt/gatekeeper/venv/bin/pip install -U pip
sudo /opt/gatekeeper/venv/bin/pip install -r /opt/gatekeeper/requirements.txt

echo "=== [8/9] Config + permissions ==="
sudo cp .env.example /etc/gatekeeper/gatekeeper.env

# Restore keys from S3 backup if available, else create empty
if aws s3 ls s3://gatekeeper-backups/keys/ 2>/dev/null; then
  LATEST=$(aws s3 ls s3://gatekeeper-backups/keys/ | sort | tail -1 | awk '{print $4}')
  sudo aws s3 cp s3://gatekeeper-backups/keys/$LATEST /etc/gatekeeper/keys.yaml
else
  echo 'apps: {}' | sudo tee /etc/gatekeeper/keys.yaml
fi

sudo chown -R gatekeeper:gatekeeper /opt/gatekeeper /etc/gatekeeper /var/log/gatekeeper
sudo chmod 700 /etc/gatekeeper
sudo chmod 600 /etc/gatekeeper/gatekeeper.env /etc/gatekeeper/keys.yaml

sudo install -m 0755 /opt/gatekeeper/scripts/gatekeeper-keys /usr/local/bin/gatekeeper-keys
sudo tee /etc/profile.d/gatekeeper.sh >/dev/null <<'EOF'
export GATEKEEPER_KEYS_FILE=/etc/gatekeeper/keys.yaml
alias gatekeeper-keys='sudo -u gatekeeper gatekeeper-keys'
EOF

echo "=== [9/9] Systemd unit ==="
sudo cp gatekeeper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gatekeeper

sleep 3
sudo systemctl status gatekeeper --no-pager | head -5
curl -s http://127.0.0.1:8080/healthz | jq

echo
echo "=== Done. Verify external reach next: ==="
echo "curl http://your-alb-0000000000.ap-south-1.elb.amazonaws.com/healthz"
```

The whole thing runs in about 20–30 minutes on a fresh g5.2xlarge, plus the model download time (~5 min for qwen3.5:9b on a reasonable connection).

To re-register the EC2 with the existing target group:

```bash
aws elbv2 register-targets \
  --region ap-south-1 \
  --target-group-arn arn:aws:elasticloadbalancing:ap-south-1:000000000000:targetgroup/gatekeeper-gw-tg/... \
  --targets Id=<new-instance-id>,Port=8080
```

(Get the exact ARN from EC2 → Target Groups → gatekeeper-gw-tg → details.)

---

## 10. Common operational tasks

**Daily**

- Stop the instance at night, start in the morning. g5.2xlarge at ₹125/hr × 14 idle hours = ₹1750/day saved.
  ```bash
  aws ec2 stop-instances --region ap-south-1 --instance-ids <i-...>
  aws ec2 start-instances --region ap-south-1 --instance-ids <i-...>
  ```
  The EIP, ALB, and gateway all auto-recover. After start, `systemctl status gatekeeper` should show active in ~60 seconds.

**Per request from dev team**

- Onboard a new app: `sudo -u gatekeeper gatekeeper-keys issue <app> --rate <n>`, deliver key out-of-band.
- Suspected compromise: `sudo -u gatekeeper gatekeeper-keys disable <app>` (instant) or `rotate` (issues new key, invalidates old).
- Decommission: `sudo -u gatekeeper gatekeeper-keys remove <app>`.

**Weekly**

- Review usage. Until proper monitoring, jq the log:
  ```bash
  sudo jq -r '.app' /var/log/gatekeeper/requests.jsonl | sort | uniq -c | sort -rn
  ```
- Confirm backup ran:
  ```bash
  aws s3 ls s3://gatekeeper-backups/keys/ | tail -7
  ```

**Quarterly**

- Rotate all API keys (90-day rotation policy):
  ```bash
  for app in $(sudo -u gatekeeper gatekeeper-keys list | awk 'NR>2 && $1!~/^$/ {print $1}'); do
    sudo -u gatekeeper gatekeeper-keys rotate "$app"
  done
  ```
- Patch OS:
  ```bash
  sudo apt update && sudo apt upgrade -y
  # if a kernel update happened: sudo reboot, then verify nvidia-smi after
  ```

---

## 11. Cost model

What's running and what it actually costs (ap-south-1 prices, approximate):

| Resource | Hourly | Monthly (24x7) | Monthly (12 hr/day) |
|---|---|---|---|
| g5.2xlarge on-demand | ~₹125 | ~₹91,000 | ~₹45,500 |
| EBS 100 GB gp3 | — | ~₹820 | ~₹820 |
| EBS 500 GB gp3 | — | ~₹4,100 | ~₹4,100 |
| EIP attached (no charge while attached + instance running) | 0 | 0 | 0 |
| EIP unattached (or attached + instance stopped) | ~₹0.45 | ~₹325 | ~₹325 |
| ALB (always-on) | ~₹2 + LCU | ~₹1,500 | ~₹1,500 |
| Data transfer out (varies) | per GB | — | — |
| S3 storage (backups) | — | ~₹0.02/GB | tiny |
| **Estimated total** | | **~₹98,000** | **~₹52,300** |

**The single biggest lever** is whether the EC2 runs 24x7 or only during work hours. Stop overnight = roughly halves the bill.

A future improvement: an EventBridge schedule + Lambda that auto-stops the instance at 8 PM IST and auto-starts at 9 AM IST on weekdays. ~15 minutes to set up, saves ~₹45,000/month vs always-on.

---

## What's next

This document covered everything below the application layer. Part 2 will cover:

- The gateway's API surface — `/healthz`, `/v1/me`, `/v1/generate`, `/v1/chat`, `/v1/classify`
- The auth model — per-app keys, header conventions, hot reload
- Rate limiting — how the token bucket works, when 429s fire
- Audit log — schema, querying with jq, future shipping to Loki
- The CLI features — issue / rotate / disable / enable / remove / list, including the SHA-256-only storage and the "plaintext shown once" rule
- Why this design is cloud-independent — what would change (and what wouldn't) to redeploy on Azure or GCP

Part 3 will walk the code file by file: `main.py`, `auth.py`, `rate_limit.py`, `proxy.py`, `models.py`, `storage.py`, `config.py`, and the `gatekeeper-keys` CLI.

Ask when you're ready for Part 2.
