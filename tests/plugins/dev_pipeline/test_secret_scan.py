"""Tests for dev-pipeline secret scanning."""

from __future__ import annotations

from plugins.dev_pipeline.pipeline import scan_diff_for_secrets

PRIVATE_KEY_DIFF = """\
diff --git a/key.pem b/key.pem
+++ b/key.pem
+-----BEGIN RSA PRIVATE KEY-----
+MIIEpAIBAAKCAQEAsecretmaterial
+-----END RSA PRIVATE KEY-----
"""

GHP_DIFF = """\
diff --git a/config.py b/config.py
+++ b/config.py
+TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
"""

SK_DIFF = """\
+api_key = "sk-live-abcdefghijklmnopqrstuv"
"""

XOXB_DIFF = """\
+slack = "xoxb-1234567890-abcdefghijklmnop"
"""

AWS_DIFF = """\
+AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
"""

ENV_DIFF = """\
+.env
+DATABASE_PASSWORD=supersecretvalue
+API_KEY=notactuallysecretname
+MY_API_KEY=alsoblocked
"""


def test_private_key_detected():
    findings = scan_diff_for_secrets(PRIVATE_KEY_DIFF)
    assert any(f["pattern"] == "private_key_pem" for f in findings)


def test_ghp_detected():
    findings = scan_diff_for_secrets(GHP_DIFF)
    assert any(f["pattern"] == "github_pat" for f in findings)


def test_sk_prefix_detected():
    findings = scan_diff_for_secrets(SK_DIFF)
    assert any(f["pattern"] == "generic_sk_prefix" for f in findings)


def test_xoxb_detected():
    findings = scan_diff_for_secrets(XOXB_DIFF)
    assert any(f["pattern"] == "slack_bot_token" for f in findings)


def test_aws_access_key_detected():
    findings = scan_diff_for_secrets(AWS_DIFF)
    assert any(f["pattern"] == "aws_access_key_id" for f in findings)


def test_env_sensitive_assignment_detected():
    findings = scan_diff_for_secrets(ENV_DIFF)
    patterns = {f["pattern"] for f in findings}
    assert "env_sensitive_assignment" in patterns


def test_findings_never_contain_secret_values():
    diff = GHP_DIFF + SK_DIFF + ENV_DIFF
    findings = scan_diff_for_secrets(diff)
    blob = str(findings)
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890" not in blob
    assert "sk-live-abcdefghijklmnopqrstuv" not in blob
    assert "supersecretvalue" not in blob


def test_ordinary_code_is_negative():
    diff = """\
diff --git a/main.py b/main.py
+++ b/main.py
+def hello():
+    return "world"
+TOKEN_NAME = "placeholder"
+example = "sk-...redacted..."
"""
    findings = scan_diff_for_secrets(diff)
    assert findings == []


def test_env_var_names_without_values_are_negative():
    diff = "+# Set PASSWORD and API_KEY in your environment\n"
    findings = scan_diff_for_secrets(diff)
    assert findings == []
