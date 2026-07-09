"""서버 공용 설정/의존성."""

import os
from pathlib import Path

DATA_ROOT = Path(os.getenv("PIVOT_DATA_DIR", "data"))
META_DIR = DATA_ROOT / "meta"
