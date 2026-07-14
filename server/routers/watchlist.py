import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator

from pivot.ingestion.fetch import OVERSEAS_EXCHANGES
from pivot.symbols.master import DOMESTIC_SYMBOL_RE

from server.deps import META_DIR

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

WATCHLIST_PATH = META_DIR / "watchlist.json"


class WatchItem(BaseModel):
    symbol: str
    name: str = ""
    region: Literal["domestic", "overseas"] = "domestic"
    exchange: str = ""

    @model_validator(mode="after")
    def validate_instrument(self):
        self.symbol = self.symbol.strip().upper()
        self.exchange = self.exchange.strip().upper()
        if self.region == "domestic":
            if not DOMESTIC_SYMBOL_RE.fullmatch(self.symbol):
                raise ValueError("domestic symbol must contain six digits")
            self.exchange = ""
        elif self.exchange not in OVERSEAS_EXCHANGES:
            raise ValueError("overseas exchange must be one of: NA, ND, NY")
        return self


def _load() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    return [
        WatchItem.model_validate(item).model_dump()
        for item in json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    ]


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
