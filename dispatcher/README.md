---
title: jobs-actions Dispatcher
emoji: 🏃
colorFrom: yellow
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
license: apache-2.0
short_description: Run GitHub Actions on Hugging Face Jobs
---

# jobs-actions Dispatcher

This Space is the dispatcher half of [jobs-actions](https://github.com/abidlabs/jobs-actions): it receives GitHub Actions `workflow_job` webhooks and launches HF Jobs to execute them.

## Configuration

Set these as **Space secrets** (Settings → Variables and secrets):

| Variable | Purpose |
|---|---|
| `GH_APP_PRIVATE_KEY` | PEM-encoded App private key. Newlines can be encoded as `\n`. |
| `GH_WEBHOOK_SECRET` | Webhook secret you set on the GitHub App |
| `HF_TOKEN` | HF token with **write** scope; used to dispatch Jobs |

Set these as regular **Space variables**:

| Variable | Purpose |
|---|---|
| `GH_APP_ID` | Your GitHub App ID (number) |
| `HF_NAMESPACE` | (optional) Namespace (user or org) under which jobs are launched & billed. Defaults to this Space's owner. |
| `ALLOWED_GITHUB_REPOSITORIES` | (recommended for public Apps) Comma-separated `owner/repo` allowlist. Webhooks from all other repositories are ignored. |
| `RUNNER_IMAGE_CPU` | (optional) Docker image for CPU jobs |
| `RUNNER_IMAGE_GPU` | (optional) Docker image for GPU jobs |
| `JOB_TIMEOUT` | (optional) Default per-job timeout, e.g. `1h` |

See [`setup/SETUP.md`](https://github.com/abidlabs/jobs-actions/blob/main/setup/SETUP.md) for the full walkthrough.

## Endpoints

- `GET /` — service metadata and supported labels
- `GET /healthz` — liveness probe
- `POST /webhook` — GitHub App webhook (HMAC-signed)
