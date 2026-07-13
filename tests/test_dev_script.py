from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dev.py"
SPEC = importlib.util.spec_from_file_location("pivot_dev_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
DEV = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEV
SPEC.loader.exec_module(DEV)


def test_api_command_uses_active_python_and_repo_import_path():
    command = DEV._api_command("127.0.0.1", 8010)

    assert command[:4] == [sys.executable, "-m", "uvicorn", "server.main:app"]
    assert command[-4:] == ["--host", "127.0.0.1", "--port", "8010"]


def test_web_command_uses_windows_npm_wrapper(monkeypatch):
    monkeypatch.setattr(DEV.shutil, "which", lambda executable: f"resolved/{executable}")
    monkeypatch.setattr(DEV, "_require_web_dependencies", lambda **kwargs: None)

    command = DEV._web_command("localhost", 5180, platform="nt")

    assert command[0] == "resolved/npm.cmd"
    assert command[1:] == ["run", "dev", "--", "--host", "localhost", "--port", "5180"]


def test_missing_vite_has_actionable_install_command(monkeypatch):
    monkeypatch.setattr(DEV.Path, "is_file", lambda self: False)
    try:
        DEV._require_web_dependencies(DEV.WEB_DIR, platform="nt")
    except SystemExit as exc:
        message = str(exc)
        assert "npm --prefix web ci --include=optional --offline=false" in message
        assert "--package-lock=false" in message
    else:
        raise AssertionError("missing Vite must stop the launcher")


def test_wildcard_bind_address_uses_loopback_for_vite_proxy():
    assert DEV._proxy_host("0.0.0.0") == "127.0.0.1"
    assert DEV._proxy_host("::") == "127.0.0.1"
    assert DEV._proxy_host("localhost") == "localhost"


def test_child_environment_loads_dotenv_without_overriding_process(monkeypatch):
    monkeypatch.setattr(
        DEV,
        "read_env_file",
        lambda: {"DOTENV_ONLY": "file", "SHARED": "file"},
    )

    env = DEV._child_environment({"PROCESS_ONLY": "process", "SHARED": "process"})

    assert env == {
        "DOTENV_ONLY": "file",
        "PROCESS_ONLY": "process",
        "SHARED": "process",
    }
