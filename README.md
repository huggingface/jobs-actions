# jobs-actions

Run your GitHub Actions workflows on **Hugging Face Jobs** with a one-line change:

```yaml
jobs:
  train:
    runs-on: hf-jobs-a10g-small   # was: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python train.py
```

GPU CI for the price of a Hub subscription. No self-hosted runner infrastructure to babysit.

## Why you might use this

Note that Jobs-Actions is in Beta, and could change. But the overall motivation for this is that GitHub-hosted runners are becoming increasingly unreliable. HF Jobs instances are reliable and offer GPU machines at much more reasonable, per-minute prices. This bridges the GitHub and HF Jobs: GitHub Actions stays in charge of orchestration, YAML, secrets, and the Checks UI; HF Jobs provides the  compute.

| | GitHub-hosted | jobs-actions (HF Jobs) | Self-hosted on EC2 |
|---|---|---|---|
| GPU access | ❌ (limited beta only) | ✅ T4, A10G, A100, H200, L4, L40s | ✅ DIY |
| Cold start | ~5–15s | ~30–90s | ~0s (warm pool) |
| Idle cost | $0 | $0 (ephemeral) | $$ |
| Setup | None | 5-minute one-time install | Days |

## How it works

```
PR push
  → GitHub fires `workflow_job.queued` webhook
  → Dispatcher (HF Space) receives it, sees `runs-on: hf-jobs-*` label
  → Mints a one-shot GitHub Actions runner registration token
  → Launches an HF Job with a base image (or your prebuilt runner image)
  → Runner registers ephemerally, GitHub dispatches the job to it
  → Runner exits after one job, container terminates
```


## Supported labels

| Label | HF Jobs flavor | Notes |
|---|---|---|
| `hf-jobs-cpu` | `cpu-basic` | 2 vCPU, 16GB RAM |
| `hf-jobs-cpu-upgrade` | `cpu-upgrade` | 8 vCPU, 32GB RAM |
| `hf-jobs-cpu-performance` | `cpu-performance` | Performance CPU tier |
| `hf-jobs-cpu-xl` | `cpu-xl` | Highest CPU tier |
| `hf-jobs-t4-small` | `t4-small` | T4 GPU (cheapest) |
| `hf-jobs-t4-medium` | `t4-medium` | T4 GPU, more RAM |
| `hf-jobs-l4x1` | `l4x1` | 1× L4 GPU |
| `hf-jobs-l4x4` | `l4x4` | 4× L4 GPU |
| `hf-jobs-a10g-small` | `a10g-small` | A10G GPU |
| `hf-jobs-a10g-large` | `a10g-large` | A10G, more RAM |
| `hf-jobs-a10g-largex2` | `a10g-largex2` | 2× A10G |
| `hf-jobs-a10g-largex4` | `a10g-largex4` | 4× A10G |
| `hf-jobs-a100-large` | `a100-large` | A100 GPU |
| `hf-jobs-a100x4` | `a100x4` | 4× A100 GPU |
| `hf-jobs-a100x8` | `a100x8` | 8× A100 GPU |
| `hf-jobs-l40sx1` | `l40sx1` | 1× L40s |
| `hf-jobs-l40sx4` | `l40sx4` | 4× L40s |
| `hf-jobs-l40sx8` | `l40sx8` | 8× L40s |
| `hf-jobs-h200` | `h200` | 1× H200 GPU |
| `hf-jobs-h200x2` | `h200x2` | 2× H200 GPU |
| `hf-jobs-h200x4` | `h200x4` | 4× H200 GPU |
| `hf-jobs-h200x8` | `h200x8` | 8× H200 GPU |
| `hf-jobs-rtx-pro-6000` | `rtx-pro-6000` | 1× RTX6000PRO GPU |
| `hf-jobs-rtx-pro-6000x2` | `rtx-pro-6000x2` | 2× RTX6000PRO GPU |
| `hf-jobs-rtx-pro-6000x4` | `rtx-pro-6000x4` | 4× RTX6000PRO GPU |
| `hf-jobs-rtx-pro-6000x8` | `rtx-pro-6000x8` | 8× RTX6000PRO GPU |


## License

Apache 2.0
