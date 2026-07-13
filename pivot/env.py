"""저장소 루트 .env 로더. 프로세스 환경변수가 파일 값보다 우선한다.

서버 전용 secret 키(예: SUPABASE_SECRET_KEY)를 읽는 유일한 통로 —
값은 절대 로그/응답에 넣지 않는다.
"""

import os
from functools import lru_cache
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


@lru_cache(maxsize=None)
def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def read_env_file(*, path: Path = ENV_PATH) -> dict[str, str]:
    """Return a copy of repository environment values without mutating os.environ."""
    return _read_env_file(path).copy()


def env_value(name: str, *, path: Path = ENV_PATH) -> str:
    return os.getenv(name) or _read_env_file(path).get(name, "")
