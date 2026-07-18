# SKE Kubernetes Backup Script

Interactive bash script to back up Kubernetes deployment configurations via `skectl` + `helm` + `kubectl`. Designed for MobaXterm local terminal (bash 4+, no `jq` required).

All cluster operations are **read-only** — the script only runs `kubectl get` and `helm get`. Nothing in the cluster is modified.

---

## Prerequisites

The following tools must be accessible in your `PATH` (or overridden via env vars — see [Configuration](#configuration)):

| Tool | Purpose |
|---|---|
| `skectl` | Authenticates and writes a kubeconfig token |
| `helm` | Lists releases and fetches resource manifests |
| `kubectl` | Fetches individual resource YAML for backup |

---

## Configuration

Before first use, open `ske_backup.sh` and fill in the three `declare -A` blocks near the top:

```bash
# ── SKE API endpoints ──────────────────────────────────────────────────────────
declare -A ENV_SKE_URL=(
    [DF_1]="https://ske.df1.your-company.com"   # ← replace
    [DF_2]="https://ske.df2.your-company.com"
    [Prod]="https://ske.prod.your-company.com"
    [DR]="https://ske.dr.your-company.com"
)

# ── Auth endpoints ─────────────────────────────────────────────────────────────
declare -A ENV_AUTH_URL=(
    [DF_1]="https://auth.df1.your-company.com"  # ← replace
    ...
)

# ── Default namespaces (user can override at runtime) ─────────────────────────
declare -A ENV_DEFAULT_NS=(
    [DF_1]="t-55547-df1"    # ← adjust to match your tenant prefix
    [DF_2]="t-55547-df2"
    [Prod]="t-55547-prod"
    [DR]="t-55547-dr"
)

# ── Optional: pre-fill username per environment ────────────────────────────────
declare -A ENV_DEFAULT_USER=(
    [DF_1]="jsmith"         # ← leave blank "" to always prompt
    ...
)
```

### Tool path overrides

If `skectl`, `helm`, or `kubectl` are not in your `PATH`, set env vars before running:

```bash
SKECTL_CMD=/opt/tools/skectl  KUBECTL_CMD=/usr/local/bin/kubectl  bash ske_backup.sh
```

---

## Usage

```bash
bash ske_backup.sh
```

The script is fully interactive — it walks you through each step with numbered menus and prompts.

---

## Step-by-step walkthrough

### Step 1 — Environment selection

```
━━━  Environment Selection  ━━━

    1)  DF_1
    2)  DF_2
    3)  Prod
    4)  DR

  Select environment [1-4]: 3
  ✓ Environment : Prod
```

### Step 2 — Namespace selection

Shows the configured default; press Enter to accept or type your own. Multiple namespaces are comma-separated.

```
━━━  Namespace Selection  ━━━

  ▸ Default for Prod : t-55547-prod
  Namespace(s) [comma-separated, Enter for default]:
  ✓ Namespaces  : t-55547-prod
```

To back up across multiple namespaces at once:

```
  Namespace(s) [comma-separated, Enter for default]: t-55547-prod, t-55547-prod-2
```

### Step 3 — Login

Password input is hidden. The password is never written to the log — it appears as `****`.

```
━━━  Login  (Prod)  ━━━

  ▸ SKE URL  : https://ske.prod.your-company.com
  ▸ Auth URL : https://auth.prod.your-company.com

  Username [jsmith]:
  Password:
  ▸ Authenticating with skectl…
  ✓ Login successful.
  ✓ kubectl context : prod-rke2-cluster
```

### Step 4 — Helm release discovery

```
━━━  Helm Release Discovery  ━━━

  ▸ Querying namespace: t-55547-prod

  #     Release                              Namespace              Chart / Status
  ──────────────────────────────────────────────────────────────────────────────
     1)  my-api        t-55547-prod    3    2024-01-15 09:00:00 UTC    deployed    my-api-2.4.1      2.4.1
     2)  my-worker     t-55547-prod    1    2024-01-14 16:30:00 UTC    deployed    my-worker-1.2.0   1.2.0
     3)  my-cron       t-55547-prod    2    2024-01-10 11:00:00 UTC    deployed    my-cron-0.9.0     0.9.0
```

### Step 5 — Release selection

```
━━━  Release Selection  ━━━

    all      →  back up every release listed above
    1        →  single release by number
    1,3,5    →  multiple releases by number

  Your choice: 1,2
  ✓ Selected 2 release(s):
      • my-api    (ns: t-55547-prod)
      • my-worker (ns: t-55547-prod)
```

### Step 5b — Manifest pre-fetch

The script fetches helm manifests immediately after release selection. This powers the component menu with real counts, and means the backup phase does not need a second round of helm calls.

```
━━━  Fetching Helm Manifests  ━━━

  ▸ Fetching: my-api  (ns: t-55547-prod)
  ✓ my-api: 9 resource(s) in manifest
  ▸ Fetching: my-worker  (ns: t-55547-prod)
  ✓ my-worker: 6 resource(s) in manifest
```

### Step 6 — Component selection

Only types that actually exist in the chosen releases are highlighted in green. Types absent from all chosen releases are shown in yellow as `(none)` — selecting them is harmless but produces nothing.

```
━━━  Component Selection  ━━━

  #     Component type                           In selected releases
  ──────────────────────────────────────────────────────────────────
  0)    All components
  1     Deployments                              3 resource(s)
  2     StatefulSets                             (none)
  3     Services                                 3 resource(s)
  4     ConfigMaps                               5 resource(s)
  5     Secrets                                  2 resource(s)
  6     Ingresses                                1 resource(s)
  7     HPAs  (HorizontalPodAutoscaler)          (none)
  8     PVCs  (PersistentVolumeClaim)            (none)
  9     CronJobs                                 (none)
  10    ServiceAccounts                          2 resource(s)
  11    Roles                                    2 resource(s)
  12    RoleBindings                             2 resource(s)
  13    PodDisruptionBudgets                     (none)
  14    NetworkPolicies                          (none)

  Enter 0 for all, or comma-separated numbers  (e.g. 1,4,5)
  Your choice: 0
```

### Step 7 — Confirm

```
━━━  Confirm Backup  ━━━

  Environment  : Prod
  Namespaces   : t-55547-prod
  Releases     : 2 selected
  Components   : Deployment StatefulSet Service ConfigMap Secret Ingress ...
  Backup root  : /home/mobaxterm/backups

  Proceed? [y/N]: y
```

### Step 8 — Backup

```
━━━  Running Backup  ━━━

  ✓ Backup directory: ske-backup_Prod_2024-01-15_143022
  ✓ Execution log   : execution_2024-01-15_143022.log

  ●  Release: my-api   (ns: t-55547-prod)
  ▸ Helm manifest: 9 resource(s) under this release
    Deployment:                         2 saved
    Service:                            2 saved
    ConfigMap:                          3 saved
    Secret:                             2 saved
    Ingress:                            1 saved  1 not found in cluster

  ●  Release: my-worker   (ns: t-55547-prod)
  ▸ Helm manifest: 6 resource(s) under this release
    Deployment:                         1 saved
    Service:                            1 saved
    ConfigMap:                          2 saved
    Secret:                             1 saved
```

### Summary

```
━━━  Done  ━━━

  ✓ Backup directory : ske-backup_Prod_2024-01-15_143022
  ✓ Files saved      : 15
  ⚠ Not found        : 1  (in helm manifest but absent from cluster)
  ✓ Manifest         : ske-backup_Prod_2024-01-15_143022/backup-manifest.txt

  To restore a single resource:
  kubectl apply -f ske-backup_Prod_2024-01-15_143022/<namespace>/<release>/<kind>/<name>.yaml

  To restore an entire release:
  kubectl apply -R -f ske-backup_Prod_2024-01-15_143022/<namespace>/<release>/
```

---

## Backup directory layout

```
ske-backup_Prod_2024-01-15_143022/
├── backup-manifest.txt               ← human-readable index of everything backed up
├── execution_2024-01-15_143022.log   ← full execution log with timestamps and commands
└── t-55547-prod/
    ├── my-api/
    │   ├── deployment/
    │   │   ├── my-api.yaml
    │   │   └── my-api-worker.yaml
    │   ├── service/
    │   │   └── my-api.yaml
    │   ├── configmap/
    │   │   ├── my-api-config.yaml
    │   │   └── my-api-env.yaml
    │   ├── secret/
    │   │   └── my-api-secret.yaml
    │   └── ingress/
    │       └── my-api.yaml
    └── my-worker/
        ├── deployment/
        │   └── my-worker.yaml
        ├── configmap/
        │   └── my-worker-config.yaml
        └── secret/
            └── my-worker-secret.yaml
```

---

## Execution log

Every command run and every resource saved or skipped is recorded in `execution_<timestamp>.log` inside the backup directory. The password is always masked.

```
[2024-01-15 14:30:35] === Login  (Prod) ===
[2024-01-15 14:30:35] CMD     skectl login https://ske.prod.example.com -s https://auth.prod.example.com -u jsmith -p ****
[2024-01-15 14:30:37] OK      Login successful.
[2024-01-15 14:30:37] CMD     kubectl config current-context
[2024-01-15 14:30:37] OK      kubectl context : prod-rke2-cluster

[2024-01-15 14:30:38] CMD     helm list -n t-55547-prod --no-headers
[2024-01-15 14:30:39] CMD     helm get manifest my-api -n t-55547-prod
[2024-01-15 14:30:40] OK      my-api: 9 resource(s) in manifest
[2024-01-15 14:30:40]   Kind breakdown across chosen releases:
[2024-01-15 14:30:40]     Deployment: 3 resource(s)
[2024-01-15 14:30:40]     Service: 3 resource(s)
[2024-01-15 14:30:40]     ConfigMap: 5 resource(s)
[2024-01-15 14:30:40]     Secret: 2 resource(s)
[2024-01-15 14:30:40]     Ingress: 1 resource(s)

[2024-01-15 14:31:02] CMD     kubectl get Deployment my-api -n t-55547-prod -o yaml
[2024-01-15 14:31:02]   SAVE  t-55547-prod/my-api  Deployment/my-api
[2024-01-15 14:31:03] CMD     kubectl get Secret my-api-secret -n t-55547-prod -o yaml
[2024-01-15 14:31:03]   SKIP  t-55547-prod/my-api  Secret/my-api-secret  (not found in cluster)

[2024-01-15 14:31:15] Total saved: 15   Skipped: 1
[2024-01-15 14:31:15] === Backup complete ===
```

---

## Restoring from backup

**Single resource:**
```bash
kubectl apply -f ske-backup_Prod_2024-01-15_143022/t-55547-prod/my-api/deployment/my-api.yaml
```

**All resources for one release:**
```bash
kubectl apply -R -f ske-backup_Prod_2024-01-15_143022/t-55547-prod/my-api/
```

**Specific component type across all releases:**
```bash
kubectl apply -f ske-backup_Prod_2024-01-15_143022/t-55547-prod/my-api/configmap/
kubectl apply -f ske-backup_Prod_2024-01-15_143022/t-55547-prod/my-worker/configmap/
```

> **Note:** Restoring Secrets and ConfigMaps alone is usually enough for a config rollback. Restoring a Deployment YAML will trigger a rollout — combine with `kubectl rollout status` to monitor progress.

---

## Notes

- **Source of truth for resource discovery is `helm get manifest`** — only resources that Helm owns are backed up. Resources created manually outside Helm will not appear.
- Resources shown as `(none)` in the component menu are not present in any of the chosen releases. Selecting them is safe — the script skips them silently.
- Resources that appear in the Helm manifest but are missing from the live cluster are logged as `SKIP` with a warning at the end. This is normal for hook-only resources (e.g. pre-install Jobs) that have already run and been cleaned up.
- Secrets are backed up as-is (base64-encoded values). Handle the backup directory with the same care as any secrets store.
