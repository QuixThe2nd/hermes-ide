"""Safe DiscordChatExporter command construction and execution."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
from typing import Callable


@dataclass(frozen=True)
class ExportRequest:
    channel_id: str
    output: Path
    after: str | None = None
    before: str | None = None


class DCEExportError(RuntimeError):
    def __init__(self, code: str, manifest: dict):
        super().__init__(code)
        self.code = code
        self.manifest = manifest


class DCEExporter:
    def __init__(self, binary: Path, *, timeout: int = 3600,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.binary = Path(binary)
        self.timeout = timeout
        self.runner = runner

    def command(self, request: ExportRequest) -> list[str]:
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise DCEExportError("dce_binary_unavailable", {"state": "error"})
        request.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(request.output.parent, 0o700)
        argv = [str(self.binary), "export", "-c", str(request.channel_id),
                "-f", "Json", "-o", str(request.output), "--utc", "--markdown", "false"]
        if request.after:
            argv.extend(["--after", request.after])
        if request.before:
            argv.extend(["--before", request.before])
        return argv

    @staticmethod
    def render_redacted(argv: list[str]) -> str:
        safe = list(argv)
        for index, value in enumerate(safe[:-1]):
            if value in {"-t", "--token"}:
                safe[index + 1] = "<redacted>"
        return " ".join(safe)

    def export(self, request: ExportRequest, token: str) -> dict:
        if not token:
            raise DCEExportError("discord_token_missing", {"state": "error"})
        argv = self.command(request)
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(request.output, flags, 0o600)
        os.close(fd)
        os.chmod(request.output, 0o600)
        manifest = {"channel_id": request.channel_id, "output": str(request.output),
                    "after": request.after, "before": request.before,
                    "command": self.render_redacted(argv), "state": "running"}
        try:
            result = self.runner(argv, shell=False, check=False, timeout=self.timeout,
                                 text=True, capture_output=True,
                                 env={**os.environ, "DOTNET_NOLOGO": "1", "DISCORD_TOKEN": token})
        except subprocess.TimeoutExpired as exc:
            manifest.update(state="error", termination_reason="timeout")
            raise DCEExportError("dce_timeout", manifest) from exc
        if result.returncode != 0:
            manifest.update(state="error", termination_reason="nonzero_exit",
                            exit_code=result.returncode)
            raise DCEExportError("dce_failed", manifest)
        if not request.output.is_file():
            manifest.update(state="error", termination_reason="output_missing")
            raise DCEExportError("dce_output_missing", manifest)
        os.chmod(request.output, 0o600)
        manifest.update(state="ok", termination_reason="exit_0", exit_code=0,
                        output_bytes=request.output.stat().st_size)
        return manifest
