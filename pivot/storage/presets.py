"""training_presets repository — 수정은 버전 증가, 삭제는 archive (docs/06 §2)."""

from __future__ import annotations

import datetime

from pivot.config import PRESET_SCHEMA_VERSION, PreprocessPreset
from pivot.storage.supabase import PostgrestClient

TABLE = "training_presets"


class PresetNotFoundError(LookupError):
    pass


class PresetConflictError(ValueError):
    pass


def resolve_stored_preset(preset_json: dict, *, schema_version: int) -> PreprocessPreset:
    """저장된 프리셋의 legacy 누락값을 재현 가능한 설정으로 materialize한다."""
    if schema_version != PRESET_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported preset schema_version {schema_version} "
            f"(expected {PRESET_SCHEMA_VERSION})"
        )
    compatible = {
        **preset_json,
        "fractal": dict(preset_json.get("fractal") or {}),
        "labeling": dict(preset_json.get("labeling") or {}),
    }
    # schema v1 초기 누락값을 새 기본값으로 해석하면 과거 결과가 달라진다.
    compatible["fractal"].setdefault("tie_policy", "all")
    compatible["labeling"].setdefault("sample_pairing", "latest_opposite_v1")
    preset = PreprocessPreset.model_validate(compatible)
    if not preset.name.strip():
        raise ValueError("saved presets require a non-empty name")
    return preset


def validate_preset(preset_json: dict, *, schema_version: int) -> PreprocessPreset:
    """하위 호환 이름. 저장 프리셋 검증은 resolver와 같은 경로를 사용한다."""
    return resolve_stored_preset(preset_json, schema_version=schema_version)


def _materialize_row(row: dict) -> dict:
    preset = resolve_stored_preset(
        row["preset"], schema_version=int(row["schema_version"])
    )
    return {**row, "preset": preset.model_dump(mode="json")}


class PresetRepository:
    def __init__(self, db: PostgrestClient) -> None:
        self.db = db

    def list(self, *, include_archived: bool = False) -> list[dict]:
        filters = {} if include_archived else {"archived_at": "is.null"}
        return [
            _materialize_row(row)
            for row in self.db.select(
                TABLE, filters=filters, order="name.asc,version.desc"
            )
        ]

    def get(self, preset_id: int) -> dict:
        rows = self.db.select(TABLE, filters={"id": f"eq.{preset_id}"})
        if not rows:
            raise PresetNotFoundError(f"preset {preset_id} not found")
        return _materialize_row(rows[0])

    def create(self, preset: PreprocessPreset) -> dict:
        """새 이름의 프리셋을 version=1로 만든다. 이름이 있으면 새 버전 생성을 유도한다."""
        validate_preset(preset.model_dump(), schema_version=PRESET_SCHEMA_VERSION)
        existing = self.db.select(
            TABLE, filters={"name": f"eq.{preset.name}"}, limit=1
        )
        if existing:
            raise PresetConflictError(
                f"preset name {preset.name!r} already exists; create a new version instead"
            )
        return self._insert(preset.name, 1, preset)

    def create_version(self, preset_id: int, preset: PreprocessPreset) -> dict:
        """기존 프리셋(id)의 이름을 유지한 채 다음 버전 행을 추가한다."""
        base = self.get(preset_id)
        name = base["name"]
        preset = preset.model_copy(update={"name": name})
        validate_preset(preset.model_dump(), schema_version=PRESET_SCHEMA_VERSION)
        versions = self.db.select(
            TABLE,
            filters={"name": f"eq.{name}"},
            order="version.desc",
            limit=1,
            columns="version",
        )
        next_version = versions[0]["version"] + 1
        return self._insert(name, next_version, preset)

    def archive(self, preset_id: int) -> dict:
        self.get(preset_id)
        rows = self.db.update(
            TABLE,
            {"archived_at": datetime.datetime.now(datetime.UTC).isoformat()},
            filters={"id": f"eq.{preset_id}", "archived_at": "is.null"},
        )
        if not rows:
            raise PresetConflictError(f"preset {preset_id} is already archived")
        return rows[0]

    def delete(self, preset_id: int) -> None:
        """smoke test 정리용 hard delete. 운영 경로는 archive를 쓴다."""
        self.db.delete(TABLE, filters={"id": f"eq.{preset_id}"})

    def _insert(self, name: str, version: int, preset: PreprocessPreset) -> dict:
        rows = self.db.insert(
            TABLE,
            {
                "name": name,
                "version": version,
                "schema_version": PRESET_SCHEMA_VERSION,
                "preset": preset.model_dump(mode="json"),
            },
        )
        return rows[0]
