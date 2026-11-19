"""The package lock must never choose an unstable Node release."""
from pm import update


def test_node_candidates_exclude_prereleases(monkeypatch):
    monkeypatch.setattr(update, "_get_json", lambda url: [
        {"version": "v26.8.0-alpha.0"}, {"version": "v26.7.0"},
        {"version": "v24.0.0-rc.1"}, {"version": "v24.20.0"},
    ])
    versions = update.node_latest_versions()
    assert versions == ["26.7.0", "24.20.0"]
