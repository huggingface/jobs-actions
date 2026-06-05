# Setup walkthrough

You need to do this **once per HF org** (which will pay for jobs) and **once per GitHub org** (which will use the App). After that, every repo in the GitHub org/user is a one-line `runs-on:` change away from using HF Jobs.

Estimated time: **~5 minutes**.

## 0. Prerequisites

- An HF account or org with billing enabled (HF Jobs is paid compute).
- A GitHub user or org where you can create Apps.
- *(Optional)* a pinned runner image. The dispatcher ships a runtime-install fallback (`ubuntu:22.04` + `apt-get` + runner download) so you can get going with **no image hosting**. For ~30s faster cold starts, fork this repo and let `.github/workflows/publish-runner-image.yml` build a prebuilt image into your GHCR namespace.

## 1. Deploy the dispatcher to an HF Space

```bash
git clone https://github.com/abidlabs/jobs-actions
cd jobs-actions

# Create the Space (Docker SDK) and push the dispatcher to it.
huggingface-cli repo create jobs-actions-dispatcher --type=space \
    --space_sdk=docker --organization=<YOUR_HF_NAMESPACE>

# Recommended for real CI so the dispatcher stays awake for GitHub webhooks.
hf spaces settings <YOUR_HF_NAMESPACE>/jobs-actions-dispatcher \
    --hardware cpu-upgrade --sleep-time -1

# Push only the dispatcher subdirectory
cd dispatcher
git init -b main
git remote add origin https://huggingface.co/spaces/<YOUR_HF_NAMESPACE>/jobs-actions-dispatcher
git add . && git commit -m "deploy dispatcher"
git push origin main
```

After ~30s the Space will build and be available at:

```
https://<YOUR_HF_NAMESPACE>-jobs-actions-dispatcher.hf.space
```

Visit it — you should see the JSON metadata response.

> **Note**: use `cpu-upgrade` for real CI so the dispatcher stays available for GitHub webhooks. `cpu-basic` is fine for testing and will probably work, but it can sleep after inactivity; if GitHub's webhook arrives while it is waking up, the workflow may stay queued until you rerun it or redeliver the webhook. The HF Jobs that *do* the work are billed separately and run on whatever flavor the workflow asks for.

## 2. Create the GitHub App

Use the manifest flow — it pre-fills permissions, events, and the webhook URL.

1. **Edit** `setup/app-manifest.json` and replace `REPLACE_WITH_SPACE_URL/webhook` with your Space URL, e.g. `https://abidlabs-jobs-actions-dispatcher.hf.space/webhook`.

2. **Open** `setup/create-app.html` in a browser (or copy the form fields into [GitHub's App-from-manifest UI](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)).

3. **Submit** — GitHub creates the App and returns you to a confirmation page. **Save the App ID, webhook secret, and the auto-generated private key (`.pem`)**.

The App needs these permissions (already in the manifest):

| Permission | Scope | Why |
|---|---|---|
| `actions` | write | Mint runner registration tokens |
| `administration` | write | Self-hosted runner management |
| `metadata` | read | Required by default |

And subscribes to **`workflow_job`** events only.

## 3. Set Space configuration

In your Space → Settings → **Variables and secrets**, add these as **Secrets**:

| Name | Value |
|---|---|
| `GH_APP_PRIVATE_KEY` | The contents of the `.pem` file. Paste the whole thing, including BEGIN/END lines. Newlines can be `\n` if the UI doesn't accept multi-line. |
| `GH_WEBHOOK_SECRET` | The webhook secret from step 2 |
| `HF_TOKEN` | An HF token with **write** scope |

Add this as a regular **Variable**:

| Name | Value |
|---|---|
| `GH_APP_ID` | The App ID from step 2 |

`HF_NAMESPACE` is optional. By default, jobs run under the owner namespace of the dispatcher Space. Set `HF_NAMESPACE` only if you want jobs billed to a different HF user or org.

Restart the Space to pick them up.

## 4. Install the App on your repo

1. Go to `https://github.com/apps/jobs-actions` (or whatever you named it).
2. Click **Install**.
3. Pick the repo(s) where you want to use HF Jobs.

That's it on the GitHub side.

## 5. Change `runs-on:`

In a workflow:

```yaml
jobs:
  test:
    runs-on: hf-jobs-cpu-upgrade   # was: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo "hello from HF Jobs"
```

Push the change. Within ~60s you should see:

- A queued workflow_job arrive at your Space's `/webhook` (check Space logs).
- An HF Job appear in your namespace's job list at `https://huggingface.co/jobs/<YOUR_HF_NAMESPACE>`.
- The runner register and the job execute.
- The workflow turn green.

## Verifying

Things to check if the workflow stays "Queued" forever:

1. **Space logs** — look for the `workflow_job` payload and any errors.
2. **HF Jobs list** — `huggingface-cli jobs list` or the web UI.
3. **Repo settings → Actions → Runners** — you should see ephemeral `hfjobs-*` runners briefly appear.
4. **App webhook deliveries** — the App settings page has a "Recent deliveries" tab with full request/response.

## Cost notes

- **Dispatcher Space**: `cpu-upgrade` is recommended for real CI. `cpu-basic` is fine for testing, but can sleep and miss webhook deliveries.
- **Per job**: HF Jobs pricing applies. cpu-basic is near-free; `t4-small` is the cheapest GPU tier (~$0.40/hr at time of writing); `a100-large` is the most expensive in common use. Each job ends when its workflow finishes — there is no idle cost.
- **Cold start**: 30–90s of HF Job startup is billed too, since the runner runs inside the job. Pre-warming is on the roadmap.

## Uninstalling

- **Per repo**: uninstall the App from the repo's settings.
- **Globally**: delete the App, delete the Space, revoke the HF token. The repo's `runs-on:` lines become orphaned (workflows queue forever) until you change them back.
