"""Inline bootstrap script that runs inside the HF Job container.

This script is fed to `bash -c` as the container command. It:

1. Installs the OS packages the GHA runner needs.
2. Downloads the pinned `actions/runner` release.
3. Configures the runner ephemerally with the credentials we passed in env.
4. Runs the runner for one job, then exits.

We use a runtime-install approach (rather than a prebuilt image) so the
dispatcher works against any public base image with apt available — no GHCR
auth or Docker Hub account required to get started. Tradeoff: ~30s of extra
cold start to download the runner each time. For repos that run lots of jobs,
fork this repo and point `RUNNER_IMAGE_*` at your own prebuilt image.
"""

from __future__ import annotations

# Pin the GitHub Actions runner version. Bump in lockstep with whatever
# GitHub is currently requiring; older versions get rejected during config.
RUNNER_VERSION = "2.336.0"

BOOTSTRAP = rf"""
set -euo pipefail

: "${{GH_REPO:?missing GH_REPO}}"
: "${{RUNNER_TOKEN:?missing RUNNER_TOKEN}}"
: "${{RUNNER_LABELS:?missing RUNNER_LABELS}}"
RUNNER_NAME="${{RUNNER_NAME:-hfjobs-$(hostname)-$(date +%s)}}"

echo "[jobs-actions] bootstrapping runner v{RUNNER_VERSION}"
echo "[jobs-actions] repo=${{GH_REPO}} labels=${{RUNNER_LABELS}} name=${{RUNNER_NAME}}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -qq -y --no-install-recommends \
    ca-certificates curl git git-lfs jq sudo \
    >/dev/null

useradd -m -s /bin/bash runner 2>/dev/null || true
echo "runner ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/runner

mkdir -p /actions-runner
chown -R runner:runner /actions-runner
cd /actions-runner

# Detect architecture for the runner download URL.
case "$(uname -m)" in
    x86_64)  ARCH=x64 ;;
    aarch64) ARCH=arm64 ;;
    *) echo "unsupported arch: $(uname -m)"; exit 1 ;;
esac

curl -fsSL -o runner.tar.gz \
    "https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/actions-runner-linux-${{ARCH}}-{RUNNER_VERSION}.tar.gz"
tar xzf runner.tar.gz
rm runner.tar.gz
chown -R runner:runner /actions-runner

# installdependencies pulls in libssl, libkrb5, etc. — required by the
# native runner host process.
sudo ./bin/installdependencies.sh >/dev/null

cleanup() {{
    if [[ -f .runner ]]; then
        sudo -u runner -E env HOME=/home/runner ./config.sh remove --token "${{RUNNER_TOKEN}}" 2>/dev/null || true
    fi
}}
trap cleanup EXIT INT TERM

sudo -u runner -E env HOME=/home/runner ./config.sh \
    --url "https://github.com/${{GH_REPO}}" \
    --token "${{RUNNER_TOKEN}}" \
    --name "${{RUNNER_NAME}}" \
    --labels "${{RUNNER_LABELS}}" \
    --no-default-labels \
    --work _work \
    --ephemeral --unattended --replace --disableupdate

# Runner exits after one job thanks to --ephemeral.
exec sudo -u runner -E env HOME=/home/runner ./run.sh
"""
