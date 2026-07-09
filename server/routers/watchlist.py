import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.deps import META_DIR

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

WATCHLIST_PATH = META_DIR / "watchlist.json"


class WatchItem(BaseModel):
    symbol: str
    name: str = ""


def _load() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    return json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))


def _save(items: list[dict]) -> None:
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@router.get("")
def list_watchlist() -> list[dict]:
    return _load()


@router.post("")
def add_watch_item(item: WatchItem) -> list[dict]:
    items = _load()
    if any(existing["symbol"] == item.symbol for existing in items):
        raise HTTPException(409, f"{item.symbol} already in watchlist")
    items.append(item.model_dump())
    _save(items)
    return items


@router.delete("/{symbol}")
def remove_watch_item(symbol: str) -> list[dict]:
    items = _load()
    remaining = [existing for existing in items if existing["symbol"] != symbol]
    if len(remaining) == len(items):
        raise HTTPException(404, f"{symbol} not in watchlist")
    _save(remaining)
    return remaining
