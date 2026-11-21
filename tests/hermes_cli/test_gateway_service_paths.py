from unittest.mock import patch


def test_service_path_skips_nonexistent_node_modules(tmp_path):
    """Service PATH should not include node_modules/.bin if it doesn't exist."""
    from hermes_cli.gateway import _build_service_path_dirs
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    node_modules_bin = str(tmp_path / "node_modules" / ".bin")
    assert node_modules_bin not in dirs


def test_service_path_includes_node_modules_when_present(tmp_path):
    """Service PATH should include node_modules/.bin when it exists."""
    nm_bin = tmp_path / "node_modules" / ".bin"
    nm_bin.mkdir(parents=True)
    from hermes_cli.gateway import _build_service_path_dirs
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    assert str(nm_bin) in dirs




def test_wsl_unit_path_persists_windows_interop_dirs(tmp_path, monkeypatch):
    """systemd units on WSL don't inherit the interop PATH, so the generated
    unit must carry the dirs holding powershell.exe/cmd.exe."""
    import hermes_cli.gateway as gw

    interop = tmp_path / "mnt" / "System32"
    interop.mkdir(parents=True)
    exe = interop / "powershell.exe"
    exe.write_text("")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{interop}:/usr/bin:/bin")
    monkeypatch.setattr(gw, "is_wsl", lambda: True)
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        unit = gw.generate_systemd_unit()
    path_line = next(line for line in unit.splitlines() if line.startswith('Environment="PATH='))
    assert str(interop) in path_line.split("=", 2)[2].strip('"').split(":")

    monkeypatch.setattr(gw, "is_wsl", lambda: False)
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        unit = gw.generate_systemd_unit()
    assert str(interop) not in unit
