"""프리셋 CRUD API. 수정은 버전 증가, 삭제는 archive (docs/06 §2).

검증/버전 규칙은 pivot.storage.presets가 소유하고 여기서는 HTTP 매핑만 한다.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pivot.config import PreprocessPreset
from pivot.storage.presets import PresetConflictError, PresetNotFoundError
from server.deps import preset_repo

router = APIRouter(prefix="/api/presets", tags=["presets"])


class PresetRequest(BaseModel):
    preset: PreprocessPreset


@router.get("")
def list_presets(include_archived: bool = False) -> list[dict]:
    return preset_repo().list(include_archived=include_archived)


@router.post("")
def create_preset(request: PresetRequest) -> dict:
    try:
        return preset_repo().create(request.preset)
    except PresetConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/{preset_id}")
def create_preset_version(preset_id: int, request: PresetRequest) -> dict:
    try:
        return preset_repo().create_version(preset_id, request.preset)
    except PresetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/{preset_id}")
def archive_preset(preset_id: int) -> dict:
    try:
        return preset_repo().archive(preset_id)
    except PresetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PresetConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/{preset_id}/permanent")
def delete_preset(preset_id: int) -> dict:
    try:
        preset_repo().delete(preset_id)
    except PresetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PresetConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"preset_id": preset_id}
