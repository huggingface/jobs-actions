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
| `RUNNER_IMAGE_<LABEL>` | (optional) Maps the case-insensitive `:<label>` suffix in `runs-on: "hf-jobs-<flavor>:<label>"` to a Docker image |
| `RUNNER_IMAGE_CPU` | (optional) Default Docker image for CPU jobs based on flavor, can also be explicitly selected with `:cpu` |
| `RUNNER_IMAGE_GPU` | (optional) Default Docker image for GPU jobs based on flavor, can also be explicitly selected with `:gpu` |
| `RUNNER_IDLE_TIMEOUT` | Seconds a registered, online runner may wait for its first job (default `300`). Checked every 30 seconds. |
| `JOB_TIMEOUT` | (optional) Default per-job timeout, e.g. `1h` |

See [`setup/SETUP.md`](https://github.com/abidlabs/jobs-actions/blob/main/setup/SETUP.md) for the full walkthrough.

## Endpoints

- `GET /` — service metadata and supported labels
- `GET /healthz` — liveness probe
- `POST /webhook` — GitHub App webhook (HMAC-signed)

## Runner lifecycle

Runner names identify compute instances; GitHub routes jobs by labels and may
assign a job to a runner launched for another workflow. The dispatcher tracks
the actual `runner_name` in start/completion events and keeps surplus runners
available briefly, then removes idle runners from GitHub before cancelling their
HF Jobs. Rejected removal never triggers HF cancellation. Busy runners are left
to finish their one job, regardless of the idle deadline.

Duplicate queued deliveries are serialized and deduplicated while a runner is
tracked. Before provisioning, the dispatcher also checks the current GitHub job
status to skip delayed deliveries for jobs that have already started or finished.

Every 30 seconds, the dispatcher also rechecks jobs received through queued
webhooks. If a runner has taken a different job or exited, it launches enough
replacements to cover the remaining queue. Idle and booting runners with the
same label count toward capacity; busy runners do not. Replacement names are
unique so they cannot replace a runner executing another job. Failed dispatches
are retried with a delay, and failed status/inventory requests do not cause
speculative launches.

Tracking is in memory and requires a single dispatcher process. A restart loses
both queued demand and existing runner tracking; jobs queued before the restart
need a new webhook/rerun to enter tracking, and existing HF jobs still rely on
`JOB_TIMEOUT`. Jobs whose queued webhook never reached this process are not
discovered by this reconciler.
The idle deadline starts when reconciliation first observes the registered runner
online, so image pulls and bootstrap time do not consume it. This cleanup does
not change HF's enforcement of the overall job timeout.
