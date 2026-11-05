"""Runtime smoke tests for Docker HOME overrides and script behavior.

Build the real image and verify the actual runtime behavior:

  1. main-wrapper preserves the Docker ``-w`` working directory
  2. stage2 hook repairs profiles/ and cron/ ownership on every boot
"""
from __future__ import annotations

import subprocess

from tests.docker.conftest import docker_exec, docker_exec_sh, start_container, restart_container




def test_stage2_repairs_profiles_and_cron_ownership(
    built_image: str, container_name: str,
) -> None:
    """profiles/ and cron/ must both be reclaimed after root-context writes.

    The stage2 hook chowns these dirs to hermes:hermes on every boot.
    We simulate a root-owned file in each, then restart the container
    and verify ownership is repaired.
    """
    start_container(built_image, container_name)

    # Create root-owned files in profiles/ and cron/ to simulate
    # docker exec (root) writes.
    docker_exec(
        container_name, "mkdir", "-p", "/opt/data/profiles/testprof",
        user="root", timeout=5,
    )
    docker_exec(
        container_name, "touch", "/opt/data/profiles/testprof/marker",
        user="root", timeout=5,
    )
    docker_exec(
        container_name, "touch", "/opt/data/cron/root_owned.json",
        user="root", timeout=5,
    )

    # Verify they're root-owned before restart.
    r = docker_exec_sh(
        container_name,
        'stat -c "%U" /opt/data/profiles/testprof/marker '
        '/opt/data/cron/root_owned.json',
        timeout=5,
    )
    assert "root" in r.stdout, (
        f"expected root-owned files before restart, got: {r.stdout!r}"
    )

    # Restart — stage2 hook runs again and repairs ownership.
    restart_container(container_name)

    # Verify files are now owned by hermes.
    r = docker_exec_sh(
        container_name,
        'stat -c "%U" /opt/data/profiles/testprof/marker '
        '/opt/data/cron/root_owned.json',
        timeout=5,
    )
    assert "hermes" in r.stdout, (
        f"expected hermes-owned files after restart, got: {r.stdout!r} — "
        f"stage2 hook did not repair profiles/ and cron/ ownership"
    )