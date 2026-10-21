"""PR #420 ``python -c`` observation-grammar regressions relocated per review comment4335516413.

Tests mirror ``cron/lifecycle_guard.py``.
"""


def test_python_c_quoted_lifecycle_phrase_is_data():
    """pc_ecfa8ae20555: a python -c one-liner that only *mentions* a
    lifecycle phrase inside a string is reading data, not restarting
    the gateway."""
    from cron.lifecycle_guard import (
        contains_gateway_lifecycle_command_or_referenced_script,
    )
    command = (
        "python3 -c "
        "\"html=open('dist/ugg-pos.html').read(); "
        "print('hermes gateway restart' in html)\""
    )
    assert contains_gateway_lifecycle_command_or_referenced_script(command) is False


def test_python_c_marker_count_one_liner_is_allowed():
    """Sibling of pc_ecfa8ae20555: counting the word restart in HTML."""
    from cron.lifecycle_guard import (
        contains_gateway_lifecycle_command_or_referenced_script,
    )
    command = (
        "python3 -c "
        "\"from pathlib import Path; "
        "html=Path('dist/ugg-pos.html').read_text(); "
        "print(html.count('restart'), html.count('stop'))\""
    )
    assert contains_gateway_lifecycle_command_or_referenced_script(command) is False


def test_python_c_os_system_lifecycle_still_blocked():
    """Masking inert -c data must not hide os.system of a lifecycle command."""
    from cron.lifecycle_guard import (
        contains_gateway_lifecycle_command_or_referenced_script,
    )
    command = "python3 -c \"import os; os.system('hermes gateway restart')\""
    assert contains_gateway_lifecycle_command_or_referenced_script(command) is True


def test_usr_bin_python_c_os_system_lifecycle_still_blocked():
    """Absolute interpreter + os.system of a systemctl lifecycle command."""
    from cron.lifecycle_guard import (
        contains_gateway_lifecycle_command_or_referenced_script,
    )
    command = (
        "/usr/bin/python3 -c "
        "\"import os; os.system('systemctl --user restart hermes-gateway')\""
    )
    assert contains_gateway_lifecycle_command_or_referenced_script(command) is True
