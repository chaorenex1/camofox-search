"""Offline tests for scripts/install_dev_plugin.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = REPO_ROOT / "plugin" / "camofox_search"


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "camofox_search_installer", REPO_ROOT / "scripts" / "install_dev_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _load_installer()


@pytest.fixture()
def hermes_home(tmp_path):
    return tmp_path / "hermes-home"


def test_dry_run_writes_nothing(hermes_home, capsys):
    rc = installer.install_plugin(
        source=PLUGIN_SOURCE, hermes_home=hermes_home, replace=False, dry_run=True)

    assert rc == 0
    assert not hermes_home.exists()
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "hash-verified" in out


def test_install_copies_and_hash_verifies(hermes_home, capsys):
    rc = installer.install_plugin(
        source=PLUGIN_SOURCE, hermes_home=hermes_home, replace=False, dry_run=False)

    assert rc == 0
    destination = hermes_home / "plugins" / "web" / "camofox_search"
    assert (destination / "plugin.yaml").is_file()
    assert (destination / "__init__.py").is_file()
    assert (destination / "provider.py").is_file()
    assert installer.collect_relative_file_hashes(destination) == \
        installer.collect_relative_file_hashes(PLUGIN_SOURCE)
    assert "next steps" in capsys.readouterr().out


def test_install_deploys_cookie_sync_script(hermes_home, capsys):
    rc = installer.install_plugin(
        source=PLUGIN_SOURCE, hermes_home=hermes_home, replace=False, dry_run=False)

    assert rc == 0
    deployed = hermes_home / "scripts" / "camofox_cookie_sync.py"
    assert deployed.is_file()
    assert deployed.read_bytes() == \
        installer.cookie_sync_script_source().read_bytes()
    assert "camofox_cookie_sync.py" in capsys.readouterr().out


def test_refuses_existing_target_without_replace(hermes_home, capsys):
    destination = hermes_home / "plugins" / "web" / "camofox_search"
    destination.mkdir(parents=True)
    marker = destination / "marker.txt"
    marker.write_text("keep me", encoding="utf-8")

    rc = installer.install_plugin(
        source=PLUGIN_SOURCE, hermes_home=hermes_home, replace=False, dry_run=False)

    assert rc == 2
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert "use --replace" in capsys.readouterr().err


def test_replace_backs_up_previous_install(hermes_home, capsys):
    destination = hermes_home / "plugins" / "web" / "camofox_search"
    destination.mkdir(parents=True)
    (destination / "old_file.txt").write_text("old", encoding="utf-8")

    rc = installer.install_plugin(
        source=PLUGIN_SOURCE, hermes_home=hermes_home, replace=True, dry_run=False)

    assert rc == 0
    backups = list((hermes_home / "backups" / "plugins" / "web").glob("camofox_search.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "old_file.txt").read_text(encoding="utf-8") == "old"
    assert not (destination / "old_file.txt").exists()  # fresh copy
    assert (destination / "provider.py").is_file()


def test_invalid_source_fails_validation(tmp_path, hermes_home, capsys):
    empty_source = tmp_path / "empty-plugin"
    empty_source.mkdir()

    rc = installer.install_plugin(
        source=empty_source, hermes_home=hermes_home, replace=False, dry_run=False)

    assert rc == 2
    assert "incomplete" in capsys.readouterr().err


def test_main_wires_argv(hermes_home, capsys):
    rc = installer.main(["--target-hermes-home", str(hermes_home), "--dry-run"])
    assert rc == 0
    assert not hermes_home.exists()


def test_required_plugin_files_present():
    for name in installer._REQUIRED_PLUGIN_FILES:
        assert (PLUGIN_SOURCE / name).is_file(), name
