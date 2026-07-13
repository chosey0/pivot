"""Cross-platform development server launcher for Pivot."""

from __future__ import annotations

import argparse
import os
import platform as platform_module
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from pivot.env import read_env_file


ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "web"


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc


def _npm_executable(platform: str = os.name) -> str:
    executable = "npm.cmd" if platform == "nt" else "npm"
    resolved = shutil.which(executable)
    if resolved is None:
        raise SystemExit(
            "npm was not found. Install Node.js 20.19+ or 22.12+ and try again."
        )
    return resolved


def _require_web_dependencies(
    web_dir: Path = WEB_DIR, *, platform: str = os.name
) -> None:
    node_modules = web_dir / "node_modules"
    vite_command = "vite.cmd" if platform == "nt" else "vite"
    required = [node_modules / ".bin" / vite_command]
    if platform == "nt" and platform_module.machine().lower() in {"amd64", "x86_64"}:
        required.extend(
            [
                node_modules / "@oxlint" / "binding-win32-x64-msvc" / "package.json",
                node_modules / "@rolldown" / "binding-win32-x64-msvc" / "package.json",
            ]
        )
    if all(path.is_file() for path in required):
        return
    raise SystemExit(
        "Web dependencies are incomplete. Run "
        "`npm --prefix web ci --include=optional --offline=false`, then on Windows x64 run "
        "`npm --prefix web install --no-save --package-lock=false --offline=false "
        "@oxlint/binding-win32-x64-msvc@1.73.0 "
        "@rolldown/binding-win32-x64-msvc@1.1.5`."
    )


def _api_command(host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "server.main:app",
        "--reload",
        "--host",
        host,
        "--port",
        str(port),
    ]


def _web_command(host: str, port: int, *, platform: str = os.name) -> list[str]:
    _require_web_dependencies(platform=platform)
    return [
        _npm_executable(platform),
        "run",
        "dev",
        "--",
        "--host",
        host,
        "--port",
        str(port),
    ]


def _proxy_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _child_environment(process_env: dict[str, str] | None = None) -> dict[str, str]:
    env = read_env_file()
    env.update(os.environ if process_env is None else process_env)
    return env


def _child_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _start(
    name: str, command: Sequence[str], cwd: Path, env: dict[str, str]
) -> subprocess.Popen:
    print(f"[{name}] starting in {cwd}: {' '.join(command)}", flush=True)
    return subprocess.Popen(command, cwd=cwd, env=env, **_child_options())


def _stop(process: subprocess.Popen, timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        if process.poll() is None:
            process.terminate()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run(processes: list[tuple[str, subprocess.Popen]]) -> int:
    try:
        while True:
            for name, process in processes:
                return_code = process.poll()
                if return_code is not None:
                    print(f"[{name}] stopped with exit code {return_code}", flush=True)
                    return return_code
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        for _, process in reversed(processes):
            _stop(process)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Pivot development servers.")
    parser.add_argument("mode", choices=("api", "web", "all"))
    parser.add_argument(
        "--host", default=os.getenv("PIVOT_HOST", os.getenv("HOST", "127.0.0.1"))
    )
    parser.add_argument("--api-port", type=int, default=_env_int("PIVOT_API_PORT", 8000))
    parser.add_argument("--web-port", type=int, default=_env_int("PIVOT_WEB_PORT", 5173))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = _child_environment()
    env.setdefault("PIVOT_API_URL", f"http://{_proxy_host(args.host)}:{args.api_port}")

    commands: list[tuple[str, list[str], Path]] = []
    if args.mode in {"api", "all"}:
        commands.append(("api", _api_command(args.host, args.api_port), ROOT_DIR))
    if args.mode in {"web", "all"}:
        commands.append(("web", _web_command(args.host, args.web_port), WEB_DIR))

    processes: list[tuple[str, subprocess.Popen]] = []
    try:
        for name, command, cwd in commands:
            processes.append((name, _start(name, command, cwd, env)))
        return _run(processes)
    except BaseException:
        for _, process in reversed(processes):
            _stop(process)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
