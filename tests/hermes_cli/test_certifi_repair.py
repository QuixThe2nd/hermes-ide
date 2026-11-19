"""Regression tests for issue #29866.

A brew Python upgrade (or an interrupted venv rebuild — see also the v0.19.0
report in the same issue) can leave ``certifi`` importable while its bundled
``cacert.pem`` is missing or a dangling symlink. Every TLS connection then
fails with an opaque ``Could not find a suitable TLS CA certificate bundle``
and the gateway is down on all platforms.

Behavior contracts pinned here:

1. The venv-repair import probes (early recovery + `hermes update`) must
   classify certifi as BROKEN when the module imports but ``cacert.pem`` is
   missing or corrupt — an attribute probe alone passes in that state.
2. ``hermes doctor`` must fail the certificate check in that state, and
   ``hermes doctor --fix`` must repair by force-reinstalling certifi and
   re-verifying.
"""

import sys
import types
from pathlib import Path

import pytest

import hermes_cli._early_recovery as er
from hermes_cli import main_install_repair


def _fake_certifi(monkeypatch, bundle_path: Path):
    """Install a fake certifi module whose where() points at bundle_path."""
    fake = types.ModuleType("certifi")
    fake.contents = lambda: ""  # satisfies the ('certifi', 'contents') probe
    fake.where = lambda: str(bundle_path)
    monkeypatch.setitem(sys.modules, "certifi", fake)
    return fake


# =========================================================================
# 1. Import probes detect a missing/corrupt cacert.pem
# =========================================================================


class TestEarlyRecoveryCertifiBundleProbe:
    def test_missing_bundle_flags_certifi_broken(self, monkeypatch, tmp_path):
        _fake_certifi(monkeypatch, tmp_path / "nonexistent" / "cacert.pem")
        broken = er._probe_broken_packages()
        assert "certifi" in broken, (
            "certifi imports but cacert.pem is missing — the probe must flag "
            "it broken (#29866); the attribute check alone passes here"
        )

    def test_tiny_bundle_flags_certifi_broken(self, monkeypatch, tmp_path):
        bundle = tmp_path / "cacert.pem"
        bundle.write_text("truncated", encoding="utf-8")
        _fake_certifi(monkeypatch, bundle)
        broken = er._probe_broken_packages()
        assert "certifi" in broken


    def test_where_raising_flags_certifi_broken(self, monkeypatch):
        fake = types.ModuleType("certifi")
        fake.contents = lambda: ""

        def _boom():
            raise OSError("simulated broken installation")

        fake.where = _boom
        monkeypatch.setitem(sys.modules, "certifi", fake)
        broken = er._probe_broken_packages()
        assert "certifi" in broken


class TestUpdateProbeScriptChecksBundle:
    """The subprocess probe used by `hermes update`'s venv repair must apply
    the same bundle-file check inside the target venv's interpreter."""

    def _run_probe_script(self, monkeypatch, tmp_path, bundle_path):
        """Extract the generated probe script and run it in-process against a
        fake certifi that points at bundle_path."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["script"] = cmd[-1]

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        monkeypatch.setattr(main_install_repair.subprocess, "run", fake_run)
        monkeypatch.setattr(
            main_install_repair, "_resolve_install_target_python", lambda *a, **k: sys.executable
        )
        main_install_repair._detect_broken_lazy_refresh_imports(["pip"])
        script = captured["script"]

        # Execute the probe script with a fake certifi installed.
        _fake_certifi(monkeypatch, bundle_path)
        printed = []
        namespace = {"__builtins__": __builtins__}
        import builtins as _b

        real_print = _b.print
        monkeypatch.setattr(
            _b, "print", lambda *a, **k: printed.append(" ".join(map(str, a)))
        )
        try:
            exec(script, namespace)
        finally:
            monkeypatch.setattr(_b, "print", real_print)
        return "\n".join(printed)


    def test_probe_script_quiet_when_bundle_healthy(self, monkeypatch, tmp_path):
        import certifi as real_certifi

        out = self._run_probe_script(
            monkeypatch, tmp_path, Path(real_certifi.where())
        )
        assert "certifi" not in out.splitlines()
