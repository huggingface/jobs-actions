"""Mapping from GitHub Actions `runs-on:` labels to HF Jobs flavors.

Labels we recognize are prefixed `hf-jobs-`. The user puts one in their
`runs-on:`; we pick it out of the workflow_job webhook payload and translate
to an HF Jobs flavor string (e.g. "cpu-basic", "a10g-small").

Any flavor in `huggingface_hub.SpaceHardware` is supported. The label form is
"hf-jobs-" + the flavor with dots/underscores normalised to dashes.
"""

from __future__ import annotations

from huggingface_hub import SpaceHardware

# Build the map programmatically so we automatically pick up new flavors as
# huggingface_hub adds them.
LABEL_TO_FLAVOR: dict[str, str] = {
    f"hf-jobs-{h.value}": h.value for h in SpaceHardware
}

# Flavors that include any GPU. We use this to pick the GPU runner image.
GPU_FLAVOR_PREFIXES = (
    "t4-",
    "a10g-",
    "a100-",
    "h100",
    "h200",
    "l4",
    "l40s",
    "inf2",
    "zero-",
    "sprx",
)


def resolve_label(labels: list[str]) -> tuple[str, str, str] | None:
    """Return the first `hf-jobs-*` label we know about plus image label and original GH label, or None."""
    for gh_label in labels:
        label = gh_label.split(":", 2) + [""]
        if label[0] in LABEL_TO_FLAVOR:
            return label[0], label[1], gh_label
    return None


def is_gpu_flavor(flavor: str) -> bool:
    return any(flavor.startswith(p) for p in GPU_FLAVOR_PREFIXES)


def supported_labels() -> list[str]:
    return sorted(LABEL_TO_FLAVOR.keys())
