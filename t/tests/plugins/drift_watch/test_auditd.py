"""auditd attribution: block parser and best-effort ausearch plumbing."""

from __future__ import annotations

import subprocess

from plugins.drift_watch.core import (
    ATTRIBUTION_UNAVAILABLE,
    AUSEARCH_MISSING,
    auditd_tail,
    parse_ausearch_blocks,
)

# Mirrors real ``ausearch -k live-tree-write -i`` output on the author's host:
# bare (unquoted) values, timestamps interpreted into ``msg=audit(...)``,
# directory name tokens carrying a trailing slash.
CANNED_AUSEARCH = """----
type=PROCTITLE msg=audit(29/08/26 13:31:00.067:17810) : proctitle=/usr/bin/python3 /root/hermes-agent/cli.py
type=PATH msg=audit(29/08/26 13:31:00.067:17810) : item=1 name=/root/hermes-agent/__pycache__/cli.cpython-313.pyc.140528317789920 inode=1 mode=file,644 nametype=CREATE
type=PATH msg=audit(29/08/26 13:31:00.067:17810) : item=0 name=/root/hermes-agent/__pycache__/ inode=2 mode=dir,755 nametype=PARENT
type=CWD msg=audit(29/08/26 13:31:00.067:17810) : cwd=/
type=SYSCALL msg=audit(29/08/26 13:31:00.067:17810) : arch=x86_64 syscall=openat success=yes exit=3 comm=python3 exe=/usr/bin/python3.13 key=live-tree-write
----
type=CONFIG_CHANGE msg=audit(29/08/26 13:31:00.500:17811) : auid=unset op=add_rule key=live-tree-write list=4 res=yes
----
type=PATH msg=audit(29/08/26 13:31:02.100:17812) : item=1 name=/root/hermes-agent/utils.py inode=3 mode=file,644 nametype=NORMAL
type=PATH msg=audit(29/08/26 13:31:02.100:17812) : item=0 name=/root/hermes-agent/ inode=4 mode=dir,755 nametype=PARENT
type=SYSCALL msg=audit(29/08/26 13:31:02.100:17812) : syscall=rename success=yes comm=bash exe=/usr/bin/bash key=live-tree-write
----
"""

# Raw (non ``-i``) ausearch shape: ``time->`` header lines and quoted values.
CANNED_QUOTED = """----
time->Sat Aug 29 10:07:00 2026
type=PATH msg=audit(1756454820.123:45): item=0 name="/root/hermes-agent/cli.py" inode=11 mode=100644
type=SYSCALL msg=audit(1756454820.123:45): arch=c000003e syscall=257 success=yes comm="python3" exe="/usr/bin/python3.11" key="live-tree-write"
----
"""


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_parser_emits_one_line_per_block_and_skips_config_change():
    assert parse_ausearch_blocks(CANNED_AUSEARCH) == [
        "29/08/26 13:31:00 | python3 | /usr/bin/python3.13 | "
        "/root/hermes-agent/__pycache__/cli.cpython-313.pyc.140528317789920",
        "29/08/26 13:31:02 | bash | /usr/bin/bash | /root/hermes-agent/utils.py",
    ]


def test_parser_drops_directory_name_tokens():
    for line in parse_ausearch_blocks(CANNED_AUSEARCH):
        for path in line.split(" | ")[-1].split(","):
            assert path == "" or not path.endswith("/")


def test_parser_joins_multiple_paths_with_commas():
    text = """----
type=PATH msg=audit(29/08/26 10:10:00.0:1) : item=0 name=/a.py
type=PATH msg=audit(29/08/26 10:10:00.0:1) : item=1 name=/b.py
type=SYSCALL msg=audit(29/08/26 10:10:00.0:1) : comm=vim exe=/usr/bin/vim
----
"""
    assert parse_ausearch_blocks(text) == [
        "29/08/26 10:10:00 | vim | /usr/bin/vim | /a.py,/b.py"
    ]


def test_parser_cuts_the_timestamp_at_the_millis():
    text = """----
type=SYSCALL msg=audit(29/08/26 10:10:00.999:42) : comm=cat exe=/usr/bin/cat name=/c.py
----
"""
    assert parse_ausearch_blocks(text) == [
        "29/08/26 10:10:00 | cat | /usr/bin/cat | /c.py"
    ]


def test_parser_handles_time_header_and_quoted_values():
    assert parse_ausearch_blocks(CANNED_QUOTED) == [
        "Sat Aug 29 10:07:00 2026 | python3 | /usr/bin/python3.11 | "
        "/root/hermes-agent/cli.py"
    ]


def test_parser_falls_back_to_epoch_msg_audit_without_time_header():
    text = """----
type=SYSCALL msg=audit(1756454820.123:45): comm="cat" exe="/usr/bin/cat" name="/c.py"
----
"""
    assert parse_ausearch_blocks(text) == [
        "1756454820 | cat | /usr/bin/cat | /c.py"
    ]


def test_parser_keeps_raw_names_including_numeric_suffixes():
    # auditd's rename dance records temp siblings like ``x.pyc.131575545307952``;
    # the reference keeps those raw rather than stripping any suffix.
    text = """----
type=PATH msg=audit(29/08/26 10:10:00.0:7) : item=1 name=gateway/x.pyc.131575545307952 mode=file,644
type=SYSCALL msg=audit(29/08/26 10:10:00.0:7) : comm=python exe=/x/python3.11
----
"""
    assert parse_ausearch_blocks(text) == [
        "29/08/26 10:10:00 | python | /x/python3.11 | gateway/x.pyc.131575545307952"
    ]


def test_parser_keeps_only_the_last_twelve_lines():
    blocks = "\n".join(
        f"----\ntype=SYSCALL msg=audit(29/08/26 10:{idx:02d}:00.0:{idx}) : "
        f"comm=c{idx} exe=/e{idx} name=/p{idx}\n"
        for idx in range(15)
    )
    lines = parse_ausearch_blocks(blocks)
    assert len(lines) == 12
    assert lines[0].startswith("29/08/26 10:03:00 | c3 ")
    assert lines[-1].startswith("29/08/26 10:14:00 | c14 ")


def test_parser_skips_empty_and_noise_blocks():
    assert parse_ausearch_blocks("") == []
    assert parse_ausearch_blocks("----\n----\n") == []
    assert parse_ausearch_blocks("random journal noise\n") == []


def test_auditd_tail_reports_missing_ausearch():
    assert auditd_tail(which=lambda _name: None) == AUSEARCH_MISSING


def test_auditd_tail_reports_subprocess_failure():
    assert auditd_tail(
        which=lambda _name: "/usr/sbin/ausearch", run_cmd=lambda argv: (1, "")
    ) == ATTRIBUTION_UNAVAILABLE


def test_auditd_tail_swallows_runner_exceptions():
    def boom(argv):
        raise OSError("auditd socket gone")

    assert auditd_tail(
        which=lambda _name: "/usr/sbin/ausearch", run_cmd=boom
    ) == ATTRIBUTION_UNAVAILABLE


def test_auditd_tail_queries_the_live_tree_write_key(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(argv, *run_args, **run_kwargs):
        seen.append(list(argv))
        return _FakeCompleted(0, CANNED_AUSEARCH)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "plugins.drift_watch.core.shutil.which", lambda name: f"/usr/sbin/{name}"
    )
    text = auditd_tail()
    assert seen == [["ausearch", "-k", "live-tree-write", "-i"]]
    assert text.splitlines()[0].endswith(
        "/root/hermes-agent/__pycache__/cli.cpython-313.pyc.140528317789920"
    )


def test_auditd_tail_reports_failure_when_ausearch_errors(monkeypatch):
    def fake_run(argv, *run_args, **run_kwargs):
        return _FakeCompleted(1, "No results\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "plugins.drift_watch.core.shutil.which", lambda name: f"/usr/sbin/{name}"
    )
    assert auditd_tail() == ATTRIBUTION_UNAVAILABLE
