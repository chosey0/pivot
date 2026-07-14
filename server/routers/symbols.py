from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from pivot.symbols.master import load_domestic_common_stocks
from pivot.symbols.supabase import (
    SupabaseDomesticMasterClient,
    SupabaseOverseasMasterClient,
)

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


class SymbolSuggestion(BaseModel):
    symbol: str
    name: str
    market: str
    score: float = 0
    exchange: str = ""


class SymbolSyncResponse(BaseModel):
    markets: list[str]
    rows: int
    table: str


@router.get("/search")
def search_symbols(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=20),
    region: str = Query("domestic", pattern="^(domestic|overseas)$"),
) -> list[SymbolSuggestion]:
    try:
        rows = (
            SupabaseDomesticMasterClient()
            if region == "domestic"
            else SupabaseOverseasMasterClient()
        ).search(q, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return [
        SymbolSuggestion(
            symbol=str(row.get("symbol", "")),
            name=str(
                row.get("name")
                or row.get("korean_name")
                or row.get("english_name")
                or ""
            ),
            market=str(row.get("market", "")),
            score=float(row.get("score") or 0),
            exchange=str(row.get("exchange") or ""),
        )
        for row in rows
    ]


@router.post("/sync")
def sync_symbols() -> SymbolSyncResponse:
    entries = load_domestic_common_stocks()
    client = SupabaseDomesticMasterClient(timeout=60.0)
    try:
        rows = client.upsert_entries(entries)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return SymbolSyncResponse(markets=["KOSPI", "KOSDAQ"], rows=rows, table=client.config.table)
