"""Offline tests for scripts/camofox_cookie_sync.py (the scheduled job script)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "camofox_cookie_sync", REPO_ROOT / "scripts" / "camofox_cookie_sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


class _FakeTool:
    def __init__(self, payload):
        self.payload = payload

    def handle_import_cookies(self, args):
        assert args["action"] == "run"
        return json.dumps(self.payload)


def test_success_summary_and_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(script, "_load_plugin", lambda: _FakeTool({
        "success": True,
        "browser": "edge",
        "profile": "Default",
        "user_id": "camofox_search",
        "export": {"stats": {"exported": 42, "expired": 3, "undecryptable": 1}},
        "import": {"sent": 42, "accepted": 42, "batches": 1, "present_after": 42},
        "verified": True,
    }))

    assert script.main([]) == 0

    out = capsys.readouterr().out
    assert "browser=edge" in out and "accepted=42" in out and "verified=True" in out
    assert "skipped" in out and "expired" in out


def test_failure_payload_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(script, "_load_plugin", lambda: _FakeTool(
        {"success": False, "error": "daemon unreachable"}))

    assert script.main([]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_nothing_to_import_warns_but_succeeds(monkeypatch, capsys):
    monkeypatch.setattr(script, "_load_plugin", lambda: _FakeTool({
        "success": True, "browser": "edge", "imported": False,
        "note": "nothing to import", "export": {"stats": {"exported": 0}},
    }))

    assert script.main([]) == 0
    assert "WARN" in capsys.readouterr().out


def test_loader_exception_exits_nonzero(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("deployed plugin not found")

    monkeypatch.setattr(script, "_load_plugin", _boom)

    assert script.main([]) == 1
    out = capsys.readouterr()
    assert "FAILED" in out.out and "RuntimeError" in out.out


def test_hermes_home_resolution_prefers_script_path(tmp_path, monkeypatch):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True)
    (tmp_path / "plugins").mkdir()
    fake_script = scripts_dir / "camofox_cookie_sync.py"
    fake_script.write_text(Path(REPO_ROOT / "scripts" / "camofox_cookie_sync.py")
                           .read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("cookie_sync_copy", fake_script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
    assert module._hermes_home() == tmp_path
