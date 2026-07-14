from brokers.kis.models.symbol import SymbolRecord

from pivot.symbols.master import _is_common_stock, load_us_symbol_master
from pivot.symbols.supabase import (
    SupabaseConfig,
    SupabaseDomesticMasterClient,
    SupabaseOverseasMasterClient,
)
from server.routers.symbols import search_symbols


def test_common_stock_filter_keeps_plain_domestic_stock():
    record = SymbolRecord(
        market="KOSPI",
        symbol="005930",
        korean_name="삼성전자",
        raw={"preferred_stock": "0", "spac": "0", "etp": "0"},
    )

    assert _is_common_stock(record)


def test_common_stock_filter_drops_preferred_stock():
    record = SymbolRecord(
        market="KOSPI",
        symbol="005935",
        korean_name="삼성전자우",
        raw={"preferred_stock": "1", "spac": "0", "etp": "0"},
    )

    assert not _is_common_stock(record)


def test_common_stock_filter_drops_kosdaq_etp_and_spac():
    etp = SymbolRecord(
        market="KOSDAQ",
        symbol="123456",
        korean_name="테스트ETP",
        raw={"preferred_stock": "0", "spac": "0", "etp_product_type": "1"},
    )
    spac = SymbolRecord(
        market="KOSDAQ",
        symbol="654321",
        korean_name="테스트스팩",
        raw={"preferred_stock": "0", "spac": "Y", "etp_product_type": ""},
    )

    assert not _is_common_stock(etp)
    assert not _is_common_stock(spac)


def test_common_stock_filter_drops_non_numeric_short_code():
    record = SymbolRecord(
        market="KOSDAQ",
        symbol="0001A0",
        korean_name="테스트",
        raw={"preferred_stock": "0", "spac": "N", "etp_product_type": ""},
    )

    assert not _is_common_stock(record)


def test_us_master_normalizes_without_raw(monkeypatch):
    record = SymbolRecord(
        market="NASDAQ",
        symbol="AAPL",
        realtime_symbol="AAPL",
        korean_name="애플",
        english_name="APPLE INC",
        security_type="2",
        currency="USD",
        exchange_id="NASD",
        exchange_code="NAS",
        exchange_name="NASDAQ",
        country_code="US",
        base_price=200,
        lot_size=1,
        downloaded_at="2026-07-13T00:00:00+00:00",
        raw={"unused": "source field"},
    )
    monkeypatch.setattr(
        "pivot.symbols.master.download_symbol_master",
        lambda market, downloaded_at: [record.with_downloaded_at(downloaded_at)],
    )

    row = load_us_symbol_master(("NASDAQ",))[0].to_supabase_row()

    assert row["symbol"] == "AAPL"
    assert row["active"] is True
    assert "raw" not in row


def test_overseas_symbol_search_returns_exchange_for_watchlist(monkeypatch):
    class SearchStub:
        def search(self, query, *, limit):
            assert (query, limit) == ("AAPL", 10)
            return [
                {
                    "symbol": "AAPL",
                    "name": "애플",
                    "market": "NASDAQ",
                    "exchange": "ND",
                    "score": 1,
                }
            ]

    monkeypatch.setattr(
        "server.routers.symbols.SupabaseOverseasMasterClient", SearchStub
    )

    result = search_symbols("AAPL", limit=10, region="overseas")

    assert result[0].model_dump() == {
        "symbol": "AAPL",
        "name": "애플",
        "market": "NASDAQ",
        "score": 1.0,
        "exchange": "ND",
    }


def test_symbol_search_retries_without_internal_whitespace(monkeypatch):
    queries = []

    class Response:
        def __init__(self, rows):
            self.rows = rows

        def raise_for_status(self):
            return None

        def json(self):
            return self.rows

    def post(*args, **kwargs):
        query = kwargs["json"]["query"]
        queries.append(query)
        return Response([] if query == "삼성 전자" else [{"symbol": "005930"}])

    monkeypatch.setattr("pivot.symbols.supabase.httpx.post", post)
    client = SupabaseDomesticMasterClient(SupabaseConfig("https://example.test", "key"))

    assert client.search("  삼성   전자  ") == [{"symbol": "005930"}]
    assert queries == ["삼성 전자", "삼성전자"]


def test_symbol_search_keeps_meaningful_whitespace_when_first_query_matches(monkeypatch):
    queries = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"symbol": "AAPL"}]

    def post(*args, **kwargs):
        queries.append(kwargs["json"]["query"])
        return Response()

    monkeypatch.setattr("pivot.symbols.supabase.httpx.post", post)
    client = SupabaseOverseasMasterClient(
        SupabaseConfig("https://example.test", "key", table="overseas_master")
    )

    assert client.search("Apple Inc") == [{"symbol": "AAPL"}]
    assert queries == ["Apple Inc"]
