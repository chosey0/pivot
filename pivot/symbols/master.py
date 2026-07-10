"""KIS 국내 종목마스터를 pivot 표준 row로 변환."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from brokers.kis.symbols import download_symbol_master
from brokers.kis.models.symbol import SymbolRecord

DOMESTIC_MARKETS = ("KOSPI", "KOSDAQ")
DOMESTIC_SYMBOL_RE = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class DomesticMasterEntry:
    symbol: str
    name: str
    market: str
    standard_code: str = ""
    security_type: str = ""
    listed_date: str = ""
    active: bool = True
    raw: dict[str, Any] | None = None

    def to_supabase_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market,
            "standard_code": self.standard_code,
            "security_type": self.security_type,
            "listed_date": self.listed_date,
            "active": self.active,
            "raw": self.raw or {},
            "updated_at": datetime.now(UTC).isoformat(),
        }


def load_domestic_common_stocks(
    markets: tuple[str, ...] = DOMESTIC_MARKETS,
) -> list[DomesticMasterEntry]:
    """KOSPI/KOSDAQ 보통주 우선 종목마스터를 내려받아 정규화한다."""

    entries: list[DomesticMasterEntry] = []
    for market in markets:
        records = download_symbol_master(market)
        entries.extend(_entry_from_record(record) for record in records if _is_common_stock(record))
    return sorted(entries, key=lambda item: (item.market, item.symbol))


def _entry_from_record(record: SymbolRecord) -> DomesticMasterEntry:
    return DomesticMasterEntry(
        symbol=record.symbol,
        name=record.korean_name,
        market=record.market,
        standard_code=record.standard_code,
        security_type=record.security_type,
        listed_date=record.listed_date,
        raw=record.raw,
    )


def _is_common_stock(record: SymbolRecord) -> bool:
    if record.market not in DOMESTIC_MARKETS:
        return False
    if not record.symbol or not record.korean_name:
        return False
    if not DOMESTIC_SYMBOL_RE.fullmatch(record.symbol):
        return False

    raw = record.raw
    if _flagged(raw.get("preferred_stock")):
        return False
    if _flagged(raw.get("spac")):
        return False
    if _flagged(raw.get("etp")):
        return False
    if raw.get("etp_product_type") not in (None, "", "0"):
        return False
    return True


def _flagged(value: str | None) -> bool:
    return str(value or "").strip().upper() in {"1", "Y", "YES", "TRUE", "T"}
