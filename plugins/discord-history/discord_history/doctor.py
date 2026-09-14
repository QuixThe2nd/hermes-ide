"""Redacted, objective prerequisite checks for the archive."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .paths import DCE_VERSION, dce_archive, dce_binary

DCE_SHA256 = "8f86bd3a2c2f4412ffbbb2dcb9348642f8f929ad94a4f290ff0f78068c44fc86"
REQUIRED_EXTENSIONS = frozenset({"pg_trgm"})


def _plugin_mapping() -> Mapping[str, Any]:
    from hermes_cli.config import load_config
    cfg = load_config() or {}
    return (((cfg.get("plugins") or {}).get("entries") or {}).get("discord-history") or {}).get("config") or {}


def _check(name: str, ok: bool, code: str = "ok", **details: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "code": "ok" if ok else code, **details}


def run_doctor(*, connector=None, config_mapping: Mapping[str, Any] | None = None,
               dce_path: Path | None = None, environ: Mapping[str, str] | None = None,
               dce_archive_path: Path | None = None, runner=subprocess.run) -> dict[str, Any]:
    """Run every check without returning token, DSN, or HMAC material."""
    from .config import ConfigError, PluginConfig, load_secrets
    checks: list[dict[str, Any]] = []
    secrets = None
    try:
        secrets = load_secrets()
        checks.append(_check("secrets", True))
        checks.append(_check("audit_hmac_key", len(secrets.audit_hmac_key) == 32,
                             "invalid_audit_hmac_key"))
    except ConfigError as exc:
        checks.extend((_check("secrets", False, exc.code),
                       _check("audit_hmac_key", False, "secrets_unavailable")))

    token_present = False
    if environ is not None:
        token_present = bool(environ.get("DISCORD_BOT_TOKEN"))
    else:
        try:
            from hermes_cli.env_loader import load_hermes_dotenv
            from hermes_constants import get_hermes_home
            from tools.discord_tool import _get_bot_token
            load_hermes_dotenv(hermes_home=get_hermes_home())
            token_present = bool(_get_bot_token())
        except Exception:
            token_present = bool(os.environ.get("DISCORD_BOT_TOKEN"))
    checks.append(_check("discord_bot_token", token_present,
                         "discord_bot_token_missing", present=token_present))
    try:
        PluginConfig.from_mapping(_plugin_mapping() if config_mapping is None else config_mapping)
        checks.append(_check("plugin_config", True))
    except Exception as exc:
        checks.append(_check("plugin_config", False, getattr(exc, "code", "invalid_plugin_config")))

    resolved_dce = dce_binary() if dce_path is None else dce_path
    default_archive = dce_archive()
    if resolved_dce.is_file():
        archive = dce_archive_path or (
            default_archive if resolved_dce == dce_binary() else resolved_dce
        )
        digest = hashlib.sha256(archive.read_bytes()).hexdigest() if archive.is_file() else ""
        checksum_ok = digest == DCE_SHA256
        checks.append(_check("dce_checksum", checksum_ok, "dce_checksum_mismatch",
                             expected_sha256=DCE_SHA256))
        try:
            proc = runner([str(resolved_dce), "--version"], shell=False, check=False,
                          capture_output=True, text=True, timeout=15)
            output = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()
            version_ok = proc.returncode == 0 and DCE_VERSION in output
        except Exception:
            version_ok = False
        checks.append(_check("dce_version", version_ok, "dce_version_mismatch",
                             expected_version=DCE_VERSION))
    else:
        checks.extend((_check("dce_checksum", False, "dce_missing", expected_sha256=DCE_SHA256),
                       _check("dce_version", False, "dce_missing", expected_version=DCE_VERSION)))

    db_ok = ext_ok = False
    if secrets is not None:
        try:
            if connector is None:
                from .db import connect
                connector = connect
            conn = connector(secrets.database_url)
            try:
                row = conn.execute("SELECT current_database()").fetchone()
                db_ok = bool(row)
                rows = conn.execute("SELECT extname FROM pg_extension WHERE extname = ANY(%s)",
                                    (list(REQUIRED_EXTENSIONS),)).fetchall()
                found = {str(r[0]) for r in rows}
                ext_ok = REQUIRED_EXTENSIONS.issubset(found)
            finally:
                conn.close()
        except Exception:
            pass
    checks.append(_check("database", db_ok, "database_unreachable"))
    checks.append(_check("extensions", ext_ok, "required_extension_missing",
                         required=sorted(REQUIRED_EXTENSIONS)))
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "dce_pinned_version": DCE_VERSION, "dce_pinned_sha256": DCE_SHA256}


def requirements_ready() -> bool:
    return bool(run_doctor()["ok"])
