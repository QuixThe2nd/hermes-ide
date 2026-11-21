"""Offline contracts against published llama.cpp release asset names.

Upstream release.yml changes 0666ad2b2b (b10356) and cff184438e (b10767),
verified against GitHub release assets on both sides of each transition.
"""

import pytest

from hermes_cli.local_runtime.binaries import BinaryResolutionError, resolve_assets


@pytest.mark.parametrize("tag,linux_suffix", [
    ("b10290", "rocm-7.2-x64.tar.gz"),
    ("b10355", "rocm-7.2-x64.tar.gz"),
    ("b10356", "rocm-7.14-x64.tar.gz"),
    ("b10679", "rocm-7.14-x64.tar.gz"),
    ("b10766", "rocm-7.14-x64.tar.gz"),
    ("b10767", "rocm-10.0-x64.tar.gz"),
    ("b10964", "rocm-10.0-x64.tar.gz"),
])
def test_hip_pin_resolves_published_release_archive(tag, linux_suffix):
    plan = resolve_assets(tag, "hip", os_name="ubuntu", arch="x64")
    assert plan.tag == tag
    assert plan.backend == "hip"
    assert plan.assets == [f"llama-{tag}-bin-ubuntu-{linux_suffix}"]


def test_hip_rejects_unpublished_arm64_archive():
    with pytest.raises(BinaryResolutionError, match="no .*HIP.*arm64"):
        resolve_assets("b10964", "hip", os_name="ubuntu", arch="arm64")
