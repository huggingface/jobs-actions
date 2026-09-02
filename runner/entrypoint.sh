#!/bin/bash
#
# Entrypoint for the jobs-actions runner container.
#
# Required env vars (set by the dispatcher):
#   GH_REPO         - "owner/name"
#   RUNNER_TOKEN    - one-shot registration token from /actions/runners/registration-token
#   RUNNER_LABELS   - comma-separated labels (e.g. "hf-jobs-a10g-small")
#   RUNNER_NAME     - unique name for this ephemeral runner
#
# Optional:
#   RUNNER_GROUP    - runner group name (defaults to "default")
#   RUNNER_WORKDIR  - working directory for the runner (defaults to "_work")

set -euo pipefail

: "${GH_REPO:?missing GH_REPO}"
: "${RUNNER_TOKEN:?missing RUNNER_TOKEN}"
: "${RUNNER_LABELS:?missing RUNNER_LABELS}"
: "${RUNNER_NAME:=hfjobs-$(hostname)-$(date +%s)}"
: "${RUNNER_GROUP:=default}"
: "${RUNNER_WORKDIR:=_work}"

echo "jobs-actions runner starting"
echo "  repo:    ${GH_REPO}"
echo "  labels:  ${RUNNER_LABELS}"
echo "  name:    ${RUNNER_NAME}"
echo "  workdir: ${RUNNER_WORKDIR}"

cd /actions-runner

cleanup() {
    echo "jobs-actions runner: cleanup"
    if [[ -f .runner ]]; then
        ./config.sh remove --token "${RUNNER_TOKEN}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

./config.sh \
    --url "https://github.com/${GH_REPO}" \
    --token "${RUNNER_TOKEN}" \
    --name "${RUNNER_NAME}" \
    --labels "${RUNNER_LABELS}" \
    --no-default-labels \
    --runnergroup "${RUNNER_GROUP}" \
    --work "${RUNNER_WORKDIR}" \
    --ephemeral \
    --unattended \
    --replace \
    --disableupdate

# `run.sh` exits cleanly after one job because of `--ephemeral`.
exec ./run.sh
