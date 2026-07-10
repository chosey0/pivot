# AGENTS.md

Guidance for AI agents working in this repository.

## What this project is

**Pivot** detects swing highs/lows in candlestick data. It labels candles with the
Williams Fractal indicator (a lagging indicator that needs `(n-1)//2` future bars to confirm)
and trains a model to predict — from past data only — whether the **last bar of a sequence
will later be confirmed as a fractal high or low**. Everything (data ingestion,
preprocessing, training, live inference) runs on a local single-user web app.

This is the successor to the legacy `Fractal` project (located at `../Fractal`).
We deliberately did **not** port the legacy code. The legacy pipeline is documented as a
spec, and we reimplement it with known defects fixed.

## Current status

**M1 (data ingestion + real chart) done**: broker-modules Kiwoom day/min/tick candles,
watchlist JSON storage, parquet cache/status, `/api/chart` real candles with MA/volume,
and Vite/React Watchlist UI are implemented and verified.

**M2 (preprocessing lab) core done**: `pivot/labeling/fractal.py` (Williams fractal,
pandas-center-rolling alignment fixed by tests, lag `(n-1)//2`, labels 0/1/2, filters),
`pivot/dataset/build.py` (`run_preprocess` shared by preview and future batch),
`POST /api/preprocess/preview`, and the Lab tab (debounced param recalc, v5 markers via
`createSeriesMarkers`, stats diff bar, sample window highlight primitive, feature preview)
are implemented and browser-verified. Remaining for M3: preset CRUD/저장, batch jobs + SSE,
datasets, diagnostics. Milestones M0–M5 are defined in `docs/04_webapp_design.md` §7.

Run dev servers: `uv run uvicorn server.main:app --reload` (port 8000) and
`cd web && npm run dev` (port 5173, proxies `/api` and `/ws` to 8000).

## Documents are the source of truth

Read these before writing any code, and **update them when a decision changes**:

| Doc | Content |
|---|---|
| `docs/01_legacy_pipeline.md` | Legacy pipeline spec (data → fractal labeling → training → live app). Reimplementation baseline. |
| `docs/02_improvement_backlog.md` | Known defects (group A: fix during reimplementation), method experiments (B), engineering (C). |
| `docs/03_data_ingestion.md` | Data ingestion via broker-modules SDK: timeframes (day / N-minute / N-tick), broker choice, caching, schema mapping. |
| `docs/04_webapp_design.md` | Web workbench design: 6 tabs, preset concept, data diagnostics, API, storage layout, milestones. |
| `docs/05_package_layout.md` | Repository/package layout: `pivot/` domain library + `server/` + `web/`, dependency extras. Authoritative for folder structure. |

Docs are written in Korean; keep them in Korean. The user communicates in Korean.

## Fixed technical decisions

- **Backend**: FastAPI, Python **3.12+**, managed with **uv**. Data fetched through
  [broker-modules](https://github.com/chosey0/broker-modules) (async SDK; Kiwoom for
  domestic candles, KIS websocket for live ticks). Credentials via env vars only —
  never commit keys.
- **Frontend**: React + TypeScript + Vite. Charts use **lightweight-charts v5**
  (`chart.addSeries(CandlestickSeries)`, markers via `createSeriesMarkers`) — do not use
  v4 APIs like `series.setMarkers`.
- **Storage**: file-based (parquet + json), no database. Layout in `docs/04_webapp_design.md` §4.
- **Timeframes** are first-class: `day` / `min{N}` / `tick{N}` (N defaults to 1; allowed N
  comes from the Kiwoom SDK). Core logic must be timeframe-agnostic.

## Architecture rules

- `pivot/` is a **pure domain package** (ingestion → labeling → dataset → models →
  training → realtime; see `docs/05_package_layout.md`) with no web dependencies.
  `server/` (FastAPI) only orchestrates it; `web/` is the UI. Add subpackages in
  implementation order — no empty placeholders.
- Single-symbol preview and batch preprocessing **must call the same `pivot` functions** —
  never duplicate the pipeline per caller.
- Live inference must reuse the same scaling/sequence-building code as training
  (the legacy project drifted apart here; don't repeat it).
- Preprocessing parameters live in named **presets**; datasets and training runs store a
  full preset snapshot for reproducibility.
- Label convention: `0` = fractal low, `1` = fractal high, `2` = ignore (MA20 < MA120 at
  the labeled bar).
- When reimplementing legacy behavior, apply backlog group A fixes (float features, no
  Time column in features, masking-safe padding, symbol-level train/val split, per-class
  metrics, …) — see `docs/02_improvement_backlog.md`.

## Conventions

- Long-running work (ingestion, batch preprocessing, training) runs as jobs with SSE
  progress; training runs in a separate process so it never blocks the event loop.
- Chart time values must be unique and ascending (lightweight-charts requirement);
  tick bars need timestamp de-confliction server-side.
- Commit only when the user asks. The user reviews design decisions — when a choice is
  genuinely open, ask instead of assuming.
