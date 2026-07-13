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
are implemented and browser-verified.

**M3-A (presets + batch datasets) done**: `pivot/storage/` owns the Supabase boundary
(PostgREST/Storage clients split, preset/job/dataset repositories), `pivot/dataset/shards.py`
+ `batch.py` build parquet shards (SHA-256 verified re-download before metadata insert,
deterministic symbol-level splits), preset CRUD is version-bump/archive only,
`POST /api/preprocess/batch` runs a durable job (jobs/job_events) streamed via
`GET /api/jobs/{id}/events` SSE, and the Datasets tab + Lab preset save are browser-verified.

**M3-B (sample browser + diagnostics + lifecycle) done**: `pivot/dataset/samples.py`
serves paged/label-filtered samples with stable global indices (metadata-only parquet
reads for the index, SHA-256 verified downloads, disposable `data/tmp/shards` cache),
`pivot/diagnostics/quality.py` + `pivot/storage/diagnostics.py` produce read-only
passed/warning/failed reports (cache/preview/dataset incl. split-leakage recheck) stored
in `diagnostic_reports`, and `pivot/storage/lifecycle.py` implements batch cancel
(cooperative checks between symbols/shards; `POST /api/jobs/{id}/cancel`), dataset
deletion (freeze object list → delete objects → delete metadata; attempt recorded as a
`dataset_delete` job) and idempotent cleanup (`POST /api/datasets/cleanup`) for orphan
objects / stale building datasets / stale jobs. The remote database includes
`supabase/migrations/20260711064111_dataset_delete_job_kind.sql` for durable deletion jobs.
The Lab and Diagnostics also expose `kronos_adapted_v1` K-line quality analysis: raw
parquet stays immutable, `report_only` is the default, and `filter` recomputes indicators,
labels, and samples independently per retained segment so samples never cross a quality
boundary. Cleaning policy and outcomes are preserved in preset snapshots and dataset
symbol metadata. New presets also default to `fractal.tie_policy=plateau_last`, collapsing
consecutive equal-price extrema to the last label; Diagnostics reports residual 90% overlap
clusters without deleting samples. Legacy stored presets without this field remain `all`.

**M4 (training + evaluation) done**: `pivot/dataset/loader.py` and shared transforms load
verified Supabase shards with sample scaling and masking-safe padding; legacy and temporal
CNN1D models train in a spawned process; run/epoch/evaluation/artifact state is durable in
Supabase; verified best checkpoints live in private Storage; `/api/runs` exposes start,
detail, SSE, stop, and prediction evaluation; and the Training tab shows live curves,
confusion matrices, per-class metrics, artifacts, and prediction markers on real candles.
The integrated flow was browser-verified with dataset 20 on MPS (1 epoch, validation 137
predictions) and the smoke run/artifact was removed afterward.

Milestones M0–M5 are defined in `docs/04_webapp_design.md` §7. Next: **M5** (live inference).

Run both dev servers on macOS, Linux, or Windows with
`uv run --extra server --extra train python scripts/dev.py all` (API 8000, web 5173).
Use mode `api` or `web` to run one server; Vite proxies `/api` and `/ws` to the selected API port.

## Documents are the source of truth

Read these before writing any code, and **update them when a decision changes**:

| Doc | Content |
|---|---|
| `docs/01_legacy_pipeline.md` | Legacy pipeline spec (data → fractal labeling → training → live app). Reimplementation baseline. |
| `docs/02_improvement_backlog.md` | Known defects (group A: fix during reimplementation), method experiments (B), engineering (C). |
| `docs/03_data_ingestion.md` | Data ingestion via broker-modules SDK: timeframes (day / N-minute / N-tick), broker choice, caching, schema mapping. |
| `docs/04_webapp_design.md` | Web workbench design: 6 tabs, preset concept, data diagnostics, API, storage layout, milestones. |
| `docs/05_package_layout.md` | Repository/package layout: `pivot/` domain library + `server/` + `web/`, dependency extras. Authoritative for folder structure. |
| `docs/06_supabase_training_storage.md` | Supabase schema, private bucket paths, lifecycle, access, and retention contract for presets through training runs. |
| `docs/07_m4_implementation_plan.md` | M4 baseline contract, parallel worktree/file ownership, training API/SSE contract, and core/UI integration order. |
| `docs/08_m5_implementation_plan.md` | M5 Kiwoom WebSocket contract, candle aggregation, live inference/API events, implementation and verification order. |

Docs are written in Korean; keep them in Korean. The user communicates in Korean.

## Fixed technical decisions

- **Backend**: FastAPI, Python **3.12+**, managed with **uv**. Data fetched through
  [broker-modules](https://github.com/chosey0/broker-modules) (async SDK; Kiwoom for
  domestic candles and Kiwoom WebSocket `0B` for live trades). Credentials via env vars only —
  never commit keys.
- **Frontend**: React + TypeScript + Vite. Charts use **lightweight-charts v5**
  (`chart.addSeries(CandlestickSeries)`, markers via `createSeriesMarkers`) — do not use
  v4 APIs like `series.setMarkers`.
- **Storage**: hybrid. Raw candle parquet and the watchlist remain local operational data.
  Presets, jobs, dataset metadata, diagnostics, runs, epochs, and evaluations use Supabase
  Postgres; dataset shards and model artifacts use private Supabase Storage. Supabase is the
  source of truth for training-related data; local copies are disposable execution cache.
  See `docs/04_webapp_design.md` §4 and `docs/06_supabase_training_storage.md`.
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
- Preprocessing parameters live in named **presets** in Supabase; datasets and training runs
  store full immutable snapshots for reproducibility.
- Only the FastAPI backend may use the Supabase secret/service-role key. The browser must
  never receive it or access private training buckets directly.
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
