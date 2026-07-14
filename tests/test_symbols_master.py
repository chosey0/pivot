from brokers.kis.models.symbol import SymbolRecord

from pivot.symbols.master import _is_common_stock, load_us_symbol_master


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
