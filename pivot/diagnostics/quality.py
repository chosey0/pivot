"""데이터 품질 진단 — 읽기 전용 품질 게이트 (docs/04 §1.4).

원천 캐시, 프리셋 미리보기 결과, 데이터셋 메타데이터를 검사해
passed/warning/failed 체크 목록을 만든다. 데이터를 수정하지 않으며,
리포트는 diagnostic_reports에 입력 스냅샷과 함께 저장해 재현 가능하게 한다.
"""

from __future__ import annotations

import pandas as pd

from pivot.cleaning.kronos import PAPER_URL, POLICY_VERSION, analyze_kline_quality
from pivot.config import CleaningConfig, Timeframe
from pivot.dataset.batch import SPLIT_METHOD, assign_splits

PASSED = "passed"
WARNING = "warning"
FAILED = "failed"
_SEVERITY = {PASSED: 0, WARNING: 1, FAILED: 2}

# 경고 임계값 — 재현성을 위해 리포트 input에 함께 기록한다
MIN_SAMPLES_PER_SYMBOL = 30
MAX_IGNORE_RATIO = 0.5
MAX_NAN_DROP_RATIO = 0.3
MAX_ZERO_VOLUME_RATIO = 0.05
MAX_DAY_GAP_DAYS = 10  # 국내 연휴(추석 등)보다 긴 공백만 의심
MAX_SYMBOL_SHARE = 0.5
MAX_CLASS2_RATIO = 0.6
DIAGNOSTIC_MA_WINDOWS = (5, 20, 120)


def check(
    check_id: str,
    status: str,
    message: str,
    *,
    symbol: str | None = None,
    data: dict | None = None,
) -> dict:
    result = {"id": check_id, "status": status, "message": message}
    if symbol is not None:
        result["symbol"] = symbol
    if data:
        result["data"] = data
    return result


def overall_status(checks: list[dict]) -> str:
    worst = max((_SEVERITY[item["status"]] for item in checks), default=0)
    return next(name for name, rank in _SEVERITY.items() if rank == worst)


def build_report(checks: list[dict], input_snapshot: dict) -> dict:
    counts = {status: 0 for status in _SEVERITY}
    for item in checks:
        counts[item["status"]] += 1
    return {
        "status": overall_status(checks),
        "summary": {**counts, "checks": len(checks)},
        "checks": checks,
        "input": input_snapshot,
    }


def diagnose_cache(
    frames: dict[str, pd.DataFrame | None],
    *,
    timeframe: str,
    ma_windows: tuple[int, ...] = DIAGNOSTIC_MA_WINDOWS,
) -> dict:
    checks: list[dict] = []
    for symbol, df in frames.items():
        checks.extend(_cache_checks(symbol, df, timeframe, ma_windows))
    return build_report(
        checks,
        {
            "target": "raw_cache",
            "timeframe": timeframe,
            "symbols": sorted(frames),
            "ma_windows": list(ma_windows),
            "thresholds": {
                "max_zero_volume_ratio": MAX_ZERO_VOLUME_RATIO,
                "max_day_gap_days": MAX_DAY_GAP_DAYS,
            },
            "cleaning": {
                "policy": POLICY_VERSION,
                "mode": "report_only",
                "reference": PAPER_URL,
            },
        },
    )


def _cache_checks(
    symbol: str, df: pd.DataFrame | None, timeframe: str, ma_windows: tuple[int, ...]
) -> list[dict]:
    if df is None or df.empty:
        return [
            check(
                "cache_exists",
                FAILED,
                f"{timeframe} 캐시가 없습니다 — 수집을 먼저 실행하세요.",
                symbol=symbol,
            )
        ]
    checks = [
        check(
            "cache_exists",
            PASSED,
            f"{len(df):,}봉 ({df.index[0].date()} ~ {df.index[-1].date()})",
            symbol=symbol,
            data={"bars": len(df)},
        )
    ]

    duplicated = int(df.index.duplicated().sum())
    checks.append(
        check(
            "time_unique",
            FAILED if duplicated else PASSED,
            f"중복 timestamp {duplicated}건" if duplicated else "timestamp 고유",
            symbol=symbol,
            data={"duplicates": duplicated},
        )
    )
    ascending = bool(df.index.is_monotonic_increasing)
    checks.append(
        check(
            "time_ascending",
            PASSED if ascending else FAILED,
            "시간 오름차순" if ascending else "시간 역전 구간이 있습니다",
            symbol=symbol,
        )
    )

    body_high = df[["Open", "Close"]].max(axis=1)
    body_low = df[["Open", "Close"]].min(axis=1)
    violations = int(((df["Low"] > body_low) | (df["High"] < body_high)).sum())
    checks.append(
        check(
            "ohlc_invariant",
            FAILED if violations else PASSED,
            f"OHLC 불변식 위반 {violations}건"
            if violations
            else "OHLC 불변식(Low ≤ Open/Close ≤ High) 통과",
            symbol=symbol,
            data={"violations": violations},
        )
    )

    for column in ("Volume", "Amount"):
        if column not in df.columns:
            continue
        negative = int((df[column] < 0).sum())
        zero_ratio = float((df[column] == 0).mean())
        if negative:
            status, message = FAILED, f"{column} 음수 {negative}건"
        elif zero_ratio > MAX_ZERO_VOLUME_RATIO:
            status, message = WARNING, f"{column} 0 비율 {zero_ratio:.1%}"
        else:
            status, message = PASSED, f"{column} 정상 (0 비율 {zero_ratio:.1%})"
        checks.append(
            check(
                f"{column.lower()}_values",
                status,
                message,
                symbol=symbol,
                data={"negative": negative, "zero_ratio": round(zero_ratio, 4)},
            )
        )

    if timeframe == "day":
        gaps = df.index.to_series().diff().dt.days.dropna()
        suspicious = int((gaps > MAX_DAY_GAP_DAYS).sum())
        max_gap = int(gaps.max()) if len(gaps) else 0
        checks.append(
            check(
                "time_gaps",
                WARNING if suspicious else PASSED,
                f"{MAX_DAY_GAP_DAYS}일 초과 공백 {suspicious}건 (최대 {max_gap}일)"
                if suspicious
                else f"의심 공백 없음 (최대 {max_gap}일)",
                symbol=symbol,
                data={"suspicious_gaps": suspicious, "max_gap_days": max_gap},
            )
        )
    else:
        checks.append(
            check(
                "time_gaps",
                PASSED,
                "공백 검사는 일봉에서만 판정합니다 (장중 휴장 구분 불가)",
                symbol=symbol,
            )
        )

    bars = len(df)
    short_windows = [window for window in ma_windows if bars < window]
    nan_ratios = {
        str(window): round(min(window - 1, bars) / bars, 4) for window in ma_windows
    }
    checks.append(
        check(
            "ma_warmup",
            WARNING if short_windows else PASSED,
            f"봉 수({bars})가 MA {short_windows} 기간보다 짧습니다"
            if short_windows
            else f"MA 워밍업 NaN 비율 {nan_ratios}",
            symbol=symbol,
            data={"nan_ratios": nan_ratios},
        )
    )
    analysis = analyze_kline_quality(
        df,
        timeframe=Timeframe.from_code(timeframe),
        config=CleaningConfig(mode="report_only"),
        required_bars=max(ma_windows, default=1),
    )
    cleaning_stats = analysis.to_stats()
    invalid_prices = cleaning_stats["reason_counts"].get("invalid_price", 0)
    findings = (
        cleaning_stats["removed_bars"] > 0
        or cleaning_stats["structural_breaks"] > 0
    )
    if invalid_prices:
        cleaning_status = FAILED
    elif findings:
        cleaning_status = WARNING
    else:
        cleaning_status = PASSED
    checks.append(
        check(
            "kronos_cleaning",
            cleaning_status,
            f"Kronos 적응형 검사: 정상 구간 {cleaning_stats['segments']}개, "
            f"제외 후보 {cleaning_stats['removed_bars']:,}봉, "
            f"구조적 경계 {cleaning_stats['structural_breaks']}건",
            symbol=symbol,
            data=cleaning_stats,
        )
    )
    return checks


def diagnose_preview(results: dict[str, dict], *, input_snapshot: dict) -> dict:
    """프리셋 적용 결과 진단. results[symbol]은 run_preprocess stats 또는 {"error"}."""
    checks: list[dict] = []
    for symbol, result in results.items():
        if "error" in result:
            checks.append(
                check("preview_ok", FAILED, str(result["error"]), symbol=symbol)
            )
            continue

        samples = int(result["samples"])
        class_counts = {str(k): int(v) for k, v in result["class_counts"].items()}
        if samples == 0:
            sample_status, sample_message = FAILED, "샘플이 생성되지 않았습니다"
        elif samples < MIN_SAMPLES_PER_SYMBOL:
            sample_status = WARNING
            sample_message = f"샘플 {samples}개 — {MIN_SAMPLES_PER_SYMBOL}개 미만"
        else:
            sample_status, sample_message = PASSED, f"샘플 {samples:,}개"
        checks.append(
            check(
                "sample_count",
                sample_status,
                sample_message,
                symbol=symbol,
                data={"samples": samples, "class_counts": class_counts},
            )
        )
        pairing = result.get("pairing_stats")
        if pairing and pairing.get("rule") == "adjacent_markers_v1":
            points = int(result["points"])
            edges = int(pairing["adjacent_edges"])
            marker_counts_ok = points == edges + int(pairing["unpaired_markers"])
            sample_counts_ok = edges == (
                samples
                + int(pairing["dropped_label2"])
                + int(result["dropped_nan"])
                + int(pairing["dropped_invalid_position"])
            )
            pairing_ok = marker_counts_ok and sample_counts_ok
            checks.append(
                check(
                    "sample_pairing",
                    PASSED if pairing_ok else FAILED,
                    "adjacent 마커·샘플 보존식 일치"
                    if pairing_ok
                    else "adjacent 마커·샘플 보존식 불일치",
                    symbol=symbol,
                    data={
                        **pairing,
                        "points": points,
                        "samples": samples,
                        "dropped_nan": int(result["dropped_nan"]),
                    },
                )
            )
        if samples == 0:
            continue

        missing = [label for label in ("0", "1") if class_counts.get(label, 0) == 0]
        ignore_ratio = class_counts.get("2", 0) / samples
        if missing:
            balance_status = WARNING
            balance_message = f"클래스 {missing}가 비어 있습니다"
        elif ignore_ratio > MAX_IGNORE_RATIO:
            balance_status = WARNING
            balance_message = f"무시(2) 비율 {ignore_ratio:.1%} — 과다"
        else:
            balance_status = PASSED
            balance_message = f"클래스 분포 {class_counts}"
        checks.append(
            check(
                "class_balance",
                balance_status,
                balance_message,
                symbol=symbol,
                data={"ignore_ratio": round(ignore_ratio, 4)},
            )
        )

        points = max(int(result["points"]), 1)
        nan_ratio = int(result["dropped_nan"]) / points
        checks.append(
            check(
                "feature_nan",
                WARNING if nan_ratio > MAX_NAN_DROP_RATIO else PASSED,
                f"NaN으로 제외된 윈도우 비율 {nan_ratio:.1%}",
                symbol=symbol,
                data={"dropped_nan": int(result["dropped_nan"]), "points": points},
            )
        )

        overlap = result.get("overlap_clusters")
        if overlap:
            unresolved = int(overlap.get("sample_clusters", 0)) > 0
            dropped = int(overlap.get("dropped_plateau_points", 0))
            checks.append(
                check(
                    "sample_overlap",
                    WARNING if unresolved else PASSED,
                    (
                        f"유사 샘플 cluster {overlap.get('sample_clusters', 0)}개, "
                        f"중복 추정 {overlap.get('redundant_samples', 0)}개"
                        if unresolved
                        else f"plateau_last 정규화로 중복 라벨 {dropped}개 제거, "
                        "잔여 overlap cluster 없음"
                    ),
                    symbol=symbol,
                    data=overlap,
                )
            )

        cleaning = result.get("cleaning")
        if cleaning:
            removed_ratio = float(cleaning.get("removed_ratio", 0))
            mode = str(cleaning.get("mode", "off"))
            retained = int(cleaning.get("retained_bars", result.get("bars", 0)))
            findings = removed_ratio > 0 or int(cleaning.get("structural_breaks", 0)) > 0
            if mode == "filter" and retained == 0:
                cleaning_status = FAILED
            elif findings:
                cleaning_status = WARNING
            else:
                cleaning_status = PASSED
            checks.append(
                check(
                    "kronos_cleaning",
                    cleaning_status,
                    f"{mode}: 유지 {retained:,}봉, 제외 후보 {removed_ratio:.1%}, "
                    f"정상 구간 {cleaning.get('segments', 0)}개",
                    symbol=symbol,
                    data=cleaning,
                )
            )
    return build_report(checks, input_snapshot)


def diagnose_dataset(
    dataset: dict,
    symbol_rows: list[dict],
    shard_rows: list[dict],
    *,
    overlap_by_symbol: dict[str, dict] | None = None,
    overlap_error: str | None = None,
) -> dict:
    checks: list[dict] = []

    status = dataset["status"]
    checks.append(
        check(
            "dataset_status",
            PASSED if status == "ready" else FAILED,
            f"데이터셋 상태 {status}"
            + ("" if status == "ready" else " — 학습에 사용할 수 없습니다"),
        )
    )

    ready_rows = [row for row in symbol_rows if row["status"] == "ready"]
    symbol_total = sum(int(row["sample_count"]) for row in ready_rows)
    total_ok = int(dataset["sample_count"]) == symbol_total
    checks.append(
        check(
            "sample_totals",
            PASSED if total_ok else FAILED,
            f"샘플 합계 {dataset['sample_count']:,} (종목 합 {symbol_total:,})"
            + ("" if total_ok else " — 불일치"),
            data={"dataset": int(dataset["sample_count"]), "symbols": symbol_total},
        )
    )

    shard_counts: dict[str, int] = {}
    for shard in shard_rows:
        shard_counts[shard["symbol"]] = (
            shard_counts.get(shard["symbol"], 0) + int(shard["row_count"])
        )
    broken = {
        row["symbol"]: {
            "sample_count": int(row["sample_count"]),
            "shard_rows": shard_counts.get(row["symbol"], 0),
        }
        for row in ready_rows
        if shard_counts.get(row["symbol"], 0) != int(row["sample_count"])
    }
    checks.append(
        check(
            "shard_integrity",
            FAILED if broken else PASSED,
            f"shard 행 수와 샘플 수 불일치: {sorted(broken)}"
            if broken
            else f"shard {len(shard_rows)}개 — 종목별 행 수 일치",
            data={"mismatch": broken} if broken else {"shards": len(shard_rows)},
        )
    )

    class_counts = {str(k): int(v) for k, v in (dataset.get("class_counts") or {}).items()}
    total = sum(class_counts.values())
    if total:
        missing = [label for label in ("0", "1") if class_counts.get(label, 0) == 0]
        class2_ratio = class_counts.get("2", 0) / total
        if missing:
            class_status = WARNING
            class_message = f"클래스 {missing}가 비어 있습니다"
        elif class2_ratio > MAX_CLASS2_RATIO:
            class_status = WARNING
            class_message = f"무시(2) 비율 {class2_ratio:.1%} — 과다"
        else:
            class_status = PASSED
            class_message = f"클래스 분포 {class_counts}"
        checks.append(
            check("class_distribution", class_status, class_message, data=class_counts)
        )

        shares = {
            row["symbol"]: int(row["sample_count"]) / total for row in ready_rows
        }
        if len(shares) > 1:
            top_symbol, top_share = max(shares.items(), key=lambda item: item[1])
            checks.append(
                check(
                    "symbol_contribution",
                    WARNING if top_share > MAX_SYMBOL_SHARE else PASSED,
                    f"기여도 최대 종목 {top_symbol} ({top_share:.1%})",
                    data={"top_symbol": top_symbol, "top_share": round(top_share, 4)},
                )
            )

    lengths = [row["length_stats"] for row in ready_rows if row.get("length_stats")]
    if lengths:
        checks.append(
            check(
                "length_distribution",
                PASSED,
                f"시퀀스 길이 {min(item['min'] for item in lengths)}"
                f"~{max(item['max'] for item in lengths)}봉",
                data={
                    "min": min(item["min"] for item in lengths),
                    "max": max(item["max"] for item in lengths),
                },
            )
        )

    snapshot = dataset.get("preset_snapshot") or {}
    snapshot_preset = snapshot.get("preset") or {}
    snapshot_labeling = snapshot_preset.get("labeling") or {}
    snapshot_pairing = snapshot_labeling.get("sample_pairing")
    sample_counts = {row["symbol"]: int(row["sample_count"]) for row in ready_rows}
    pairing_rows = [
        (row["symbol"], row["length_stats"], row["length_stats"].get("pairing_stats"))
        for row in ready_rows
        if (row.get("length_stats") or {}).get("pairing_stats")
    ]
    if snapshot_pairing or pairing_rows:
        expected_rule = snapshot_pairing or "latest_opposite_v1"
        mismatched: dict[str, str | None] = {}
        broken: list[str] = []
        for symbol, length_stats, pairing in pairing_rows:
            if pairing.get("rule") != expected_rule:
                mismatched[symbol] = pairing.get("rule")
            if expected_rule == "adjacent_markers_v1" and {
                "points",
                "dropped_nan",
            }.issubset(length_stats):
                edges = int(pairing["adjacent_edges"])
                if int(length_stats["points"]) != edges + int(
                    pairing["unpaired_markers"]
                ) or edges != (
                    sample_counts[symbol]
                    + int(pairing["dropped_label2"])
                    + int(length_stats["dropped_nan"])
                    + int(pairing["dropped_invalid_position"])
                ):
                    broken.append(symbol)
        missing = sorted(
            row["symbol"]
            for row in ready_rows
            if not (row.get("length_stats") or {}).get("pairing_stats")
        )
        pairing_ok = not mismatched and not broken and not missing
        checks.append(
            check(
                "sample_pairing",
                PASSED if pairing_ok else FAILED,
                f"pairing {expected_rule} 메타데이터 일치"
                if pairing_ok
                else "pairing 메타데이터 불일치 또는 누락",
                data={
                    "rule": expected_rule,
                    "mismatched": mismatched,
                    "broken_conservation": sorted(broken),
                    "missing": missing,
                },
            )
        )

    if overlap_error:
        checks.append(
            check(
                "sample_overlap",
                FAILED,
                f"overlap cluster 계산 실패: {overlap_error}",
            )
        )
    elif overlap_by_symbol is not None:
        clusters = sum(int(item["clusters"]) for item in overlap_by_symbol.values())
        clustered = sum(
            int(item["clustered_samples"]) for item in overlap_by_symbol.values()
        )
        redundant = sum(
            int(item["redundant_samples"]) for item in overlap_by_symbol.values()
        )
        approximate = any(bool(item.get("approximate")) for item in overlap_by_symbol.values())
        top_symbol, top_stats = max(
            overlap_by_symbol.items(),
            key=lambda item: int(item[1]["redundant_samples"]),
            default=(None, {"redundant_samples": 0, "max_cluster_size": 0}),
        )
        checks.append(
            check(
                "sample_overlap",
                WARNING if clusters else PASSED,
                f"overlap cluster {clusters}개, 중복 추정 {redundant}개"
                + (
                    f" (최대 {top_symbol}: {top_stats['redundant_samples']}개)"
                    if clusters and top_symbol
                    else ""
                )
                + (" — 위치 메타가 없는 기존 shard는 같은 시작 시각 기준 근사치" if approximate else ""),
                data={
                    "clusters": clusters,
                    "clustered_samples": clustered,
                    "redundant_samples": redundant,
                    "top_symbol": top_symbol,
                    "max_cluster_size": max(
                        (int(item["max_cluster_size"]) for item in overlap_by_symbol.values()),
                        default=0,
                    ),
                    "approximate": approximate,
                    "symbols": overlap_by_symbol,
                },
            )
        )

    cleaning_rows = [
        row["length_stats"]["cleaning"]
        for row in ready_rows
        if (row.get("length_stats") or {}).get("cleaning")
    ]
    snapshot_cleaning = (
        ((dataset.get("preset_snapshot") or {}).get("preset") or {}).get("cleaning")
        or {"mode": "off"}
    )
    if cleaning_rows:
        removed = sum(int(item.get("removed_bars", 0)) for item in cleaning_rows)
        original = sum(int(item.get("original_bars", 0)) for item in cleaning_rows)
        checks.append(
            check(
                "kronos_cleaning",
                WARNING if removed else PASSED,
                f"{snapshot_cleaning.get('mode', 'off')}: {len(cleaning_rows)}종목, "
                f"제외 {removed:,}/{original:,}봉",
                data={
                    "policy": snapshot_cleaning,
                    "symbols": len(cleaning_rows),
                    "original_bars": original,
                    "removed_bars": removed,
                },
            )
        )
    else:
        checks.append(
            check(
                "kronos_cleaning",
                WARNING if snapshot_cleaning.get("mode") == "filter" else PASSED,
                "클리닝 적용 통계가 없습니다"
                if snapshot_cleaning.get("mode") == "filter"
                else "클리닝 미적용 데이터셋",
                data={"policy": snapshot_cleaning},
            )
        )

    checks.extend(_split_checks(dataset, symbol_rows))
    return build_report(
        checks,
        {
            "target": "dataset",
            "dataset_id": dataset["id"],
            "dataset_name": dataset["name"],
            "preset_snapshot": dataset.get("preset_snapshot"),
        },
    )


def _split_checks(dataset: dict, symbol_rows: list[dict]) -> list[dict]:
    """종목 단위 split 누수/규칙 위반 검사 (백로그 A5)."""
    checks: list[dict] = []
    symbols = [row["symbol"] for row in symbol_rows]
    if len(set(symbols)) != len(symbols):
        checks.append(
            check("split_leakage", FAILED, "같은 종목이 여러 행에 존재합니다 — 누수 위험")
        )
        return checks
    missing = sorted(row["symbol"] for row in symbol_rows if row["split"] is None)
    if missing:
        checks.append(
            check(
                "split_leakage",
                FAILED,
                f"split 미배정 종목: {missing}",
                data={"missing": missing},
            )
        )
        return checks

    split_conf = (dataset.get("preset_snapshot") or {}).get("split") or {}
    if split_conf.get("method") != SPLIT_METHOD:
        checks.append(
            check(
                "split_leakage",
                WARNING,
                f"알 수 없는 split 규칙 {split_conf.get('method')!r} — 재계산 검증 불가",
            )
        )
        return checks

    expected = assign_splits(
        symbols, seed=int(split_conf["seed"]), ratios=split_conf["ratios"]
    )
    actual = {row["symbol"]: row["split"] for row in symbol_rows}
    mismatched = sorted(
        symbol for symbol, split in actual.items() if expected.get(symbol) != split
    )
    if mismatched:
        checks.append(
            check(
                "split_leakage",
                FAILED,
                f"split 배정이 스냅샷 규칙과 다릅니다 (누수 위험): {mismatched}",
                data={"mismatched": mismatched},
            )
        )
    else:
        counts = {
            name: sum(1 for split in actual.values() if split == name)
            for name in ("train", "validation", "test")
        }
        empty = [name for name in ("validation", "test") if counts[name] == 0]
        checks.append(
            check(
                "split_leakage",
                WARNING if empty else PASSED,
                f"종목 단위 split 규칙 일치 {counts}"
                + (f" — {empty} 비어 있음" if empty else ""),
                data=counts,
            )
        )
    return checks
