import json
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator

from pivot.config import MINUTE_UNITS, TICK_UNITS, Timeframe
from pivot.ingestion.cache import cache_path, delete_cache
from pivot.ingestion.fetch import OVERSEAS_EXCHANGES, cache_broker
from pivot.symbols.master import DOMESTIC_SYMBOL_RE

from server.deps import DATA_ROOT, META_DIR

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

WATCHLIST_PATH = META_DIR / "watchlist.json"


class WatchItem(BaseModel):
    symbol: str
    name: str = ""
    region: Literal["domestic", "overseas"] = "domestic"
    exchange: str = ""
    timeframe: str = "day"
    start: date | datetime | None = None
    end: date | datetime | None = None

    @model_validator(mode="after")
    def validate_instrument(self):
        self.symbol = self.symbol.strip().upper()
        self.exchange = self.exchange.strip().upper()
        self.timeframe = Timeframe.from_code(self.timeframe).code
        if self.region == "domestic":
            if not DOMESTIC_SYMBOL_RE.fullmatch(self.symbol):
                raise ValueError("domestic symbol must contain six digits")
            self.exchange = ""
        elif self.exchange not in OVERSEAS_EXCHANGES:
            raise ValueError("overseas exchange must be one of: NA, ND, NY")
        if self.start is not None and self.end is not None:
            start = _as_datetime(self.start, end_of_day=False)
            end = _as_datetime(self.end, end_of_day=True)
            if start > end:
                raise ValueError("start must be on or before end")
        return self


def _as_datetime(value: date | datetime, *, end_of_day: bool) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.max.time() if end_of_day else datetime.min.time())


def _identity(item: WatchItem | dict) -> tuple:
    row = item.model_dump(mode="json") if isinstance(item, WatchItem) else item
    return tuple(
        row.get(field) for field in ("region", "exchange", "symbol", "timeframe", "start", "end")
    )


def _load() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    items: list[dict] = []
    for raw in json.loads(WATCHLIST_PATH.read_text(encoding="utf-8")):
        item = WatchItem.model_validate(raw)
        timeframes = [item.timeframe] if "timeframe" in raw else _cached_timeframes(item)
        items.extend(
            item.model_copy(update={"timeframe": code}).model_dump(mode="json")
            for code in timeframes
        )
    return items


def _cached_timeframes(item: WatchItem) -> list[str]:
    broker = cache_broker(item.region, item.exchange)
    codes = [
        "day",
        *(f"min{unit}" for unit in MINUTE_UNITS),
        *(f"tick{unit}" for unit in TICK_UNITS),
    ]
    cached = [
        code
        for code in codes
        if cache_path(DATA_ROOT, broker, code, item.symbol).exists()
    ]
    return cached or ["day"]


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
    if any(_identity(existing) == _identity(item) for existing in items):
        raise HTTPException(409, f"same data item already exists for {item.symbol}")
    items.append(item.model_dump(mode="json"))
    _save(items)
    return items


@router.delete("/{symbol}")
def remove_watch_item(
    symbol: str,
    timeframe: str = "day",
    region: Literal["domestic", "overseas"] = "domestic",
    exchange: str = "",
    start: date | datetime | None = None,
    end: date | datetime | None = None,
) -> list[dict]:
    target = WatchItem(
        symbol=symbol,
        region=region,
        exchange=exchange,
        timeframe=timeframe,
        start=start,
        end=end,
    )
    items = _load()
    remaining = [existing for existing in items if _identity(existing) != _identity(target)]
    if len(remaining) == len(items):
        raise HTTPException(404, f"data item not found for {symbol}")
    cache_identity = _identity(target)[:4]
    if not any(_identity(existing)[:4] == cache_identity for existing in remaining):
        delete_cache(
            cache_path(
                DATA_ROOT,
                cache_broker(target.region, target.exchange),
                target.timeframe,
                target.symbol,
            )
        )
    _save(remaining)
    return remaining
