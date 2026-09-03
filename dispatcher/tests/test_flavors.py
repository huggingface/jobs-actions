from dispatcher.flavors import (
    LABEL_TO_FLAVOR,
    is_gpu_flavor,
    resolve_label,
    supported_labels,
)


def test_label_map_includes_common_flavors():
    assert "hf-jobs-cpu-basic" in LABEL_TO_FLAVOR
    assert LABEL_TO_FLAVOR["hf-jobs-cpu-basic"] == "cpu-basic"
    assert "hf-jobs-a10g-small" in LABEL_TO_FLAVOR
    assert LABEL_TO_FLAVOR["hf-jobs-a10g-small"] == "a10g-small"
    assert "hf-jobs-t4-small" in LABEL_TO_FLAVOR


def test_resolve_label_finds_first_match():
    labels = resolve_label(["self-hosted", "hf-jobs-cpu-basic"])
    assert labels
    assert labels[0] == "hf-jobs-cpu-basic"
    labels = resolve_label(["self-hosted", "hf-jobs-cpu-basic:gpu"])
    assert labels
    assert labels[0] == "hf-jobs-cpu-basic"
    assert labels[1] == "gpu"
    assert labels[2] == "hf-jobs-cpu-basic:gpu"


def test_resolve_label_returns_none_when_no_match():
    assert resolve_label(["ubuntu-latest"]) is None
    assert resolve_label([]) is None
    # Unknown hf-jobs label that isn't in the SpaceHardware enum
    assert resolve_label(["hf-jobs-not-a-real-flavor"]) is None


def test_is_gpu_flavor_classification():
    assert not is_gpu_flavor("cpu-basic")
    assert not is_gpu_flavor("cpu-upgrade")
    assert is_gpu_flavor("a10g-small")
    assert is_gpu_flavor("a10g-large")
    assert is_gpu_flavor("a100-large")
    assert is_gpu_flavor("t4-small")
    assert is_gpu_flavor("t4-medium")
    assert is_gpu_flavor("h200")
    assert is_gpu_flavor("l4x1")
    assert is_gpu_flavor("l40sx1")
    assert is_gpu_flavor("zero-a10g")


def test_supported_labels_is_sorted_and_nonempty():
    labels = supported_labels()
    assert labels == sorted(labels)
    assert len(labels) > 5
    assert all(label.startswith("hf-jobs-") for label in labels)
