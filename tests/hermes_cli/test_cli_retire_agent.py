"""The interactive CLI must release the old AIAgent's LLM clients when it drops the instance
for a rebuild (model/route/credential change, MoA one-shot):
on the codex_app_server route the app-server child belongs to that instance and ``self.agent =
None`` alone orphans it for the CLI process lifetime (#72548)."""

from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class _FakeAgent:
    def __init__(self):
        self.release_calls = 0

    def release_clients(self):
        self.release_calls += 1
