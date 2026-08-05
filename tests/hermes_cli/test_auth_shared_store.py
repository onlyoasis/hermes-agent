from pathlib import Path

from hermes_cli import auth


def test_auth_file_override_uses_one_absolute_store_for_profile_processes(
    tmp_path: Path, monkeypatch
) -> None:
    shared_auth_file = tmp_path / "shared" / "auth.json"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "architect"))
    monkeypatch.setenv("HERMES_AUTH_FILE", str(shared_auth_file))

    assert auth._auth_file_path() == shared_auth_file
    assert auth._auth_lock_path() == shared_auth_file.with_suffix(".lock")


def test_empty_auth_file_override_keeps_profile_local_store(
    tmp_path: Path, monkeypatch
) -> None:
    profile_home = tmp_path / "profiles" / "operator"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_AUTH_FILE", "   ")

    assert auth._auth_file_path() == profile_home / "auth.json"
