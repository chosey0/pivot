export interface Candle {
  time: string | number // 일봉: 'yyyy-mm-dd', 분/틱봉: unix timestamp(초)
  open: number
  high: number
  low: number
  close: number
}

export interface VolumePoint {
  time: string | number
  value: number
}

export interface LinePoint {
  time: string | number
  value: number
}

export interface ChartResponse {
  symbol: string
  timeframe: string
  candles: Candle[]
  volumes: VolumePoint[]
  ma: Record<string, LinePoint[]>
  has_more?: boolean
  next_before?: string | null
}

export interface WatchItem {
  symbol: string
  name: string
}

export interface SymbolSuggestion {
  symbol: string
  name: string
  market: string
  score: number
}

export interface CacheStatus {
  bars: number
  first: string
  last: string
  updated_at: string
}

export interface IngestResult {
  ok: boolean
  bars?: number
  error?: string
}

export interface IngestResponse {
  timeframe: string
  results: Record<string, IngestResult>
}

export interface IngestOptions {
  start?: string
  end?: string
}

export interface ChartOptions {
  limit?: number
  before?: string | number
}

export interface PreviewMarker {
  time: string | number
  position: number
  kind: 'low' | 'high'
  label: 0 | 1 | 2
  price: number
}

export interface PreviewSample {
  index: number
  label: 0 | 1 | 2
  kind: 'low' | 'high'
  length: number
  start_time: string | number
  end_time: string | number
  start_position: number
  end_position: number
}

export interface PreviewStats {
  bars: number
  points: number
  samples: number
  class_counts: Record<string, number>
  dropped_nan: number
  dropped_unpaired: number
  dropped_filters: number
  dropped_ignore: number
  swing_ignored: number
  confirmation_lag: number
  overlap_clusters: OverlapClusterStats
  cleaning: CleaningStats
}

export type FractalTiePolicy = 'all' | 'plateau_last'

export interface OverlapClusterStats {
  tie_policy: FractalTiePolicy
  plateau_clusters: number
  plateau_clustered_points: number
  dropped_plateau_points: number
  max_plateau_cluster_size: number
  sample_clusters: number
  clustered_samples: number
  redundant_samples: number
  max_sample_cluster_size: number
  threshold: number
  max_end_gap: number
}

export type CleaningMode = 'off' | 'report_only' | 'filter'

export interface CleaningStats {
  mode: CleaningMode
  policy: 'kronos_adapted_v1'
  reference: string
  original_bars: number
  retained_bars: number
  removed_bars: number
  removed_ratio: number
  segments: number
  segment_lengths: number[]
  structural_breaks: number
  reason_counts: Record<string, number>
  thresholds: Record<string, unknown>
}

export interface PreviewFeatures {
  columns: string[]
  dimension: number
}

export interface PreviewResponse extends ChartResponse {
  markers: PreviewMarker[]
  samples: PreviewSample[]
  stats: PreviewStats
  features: PreviewFeatures
}

export interface PreviewParams {
  timeframe: { type: 'day' | 'minute' | 'tick'; unit: number }
  fractal: { n: number; tie_policy: FractalTiePolicy }
  ma_windows: number[]
  features: string[]
  labeling: {
    mode: 'cls3' | 'cls2_drop'
    ignore_rule: 'ma20<ma120' | 'none'
    ignore_swing_pct: number | null
  }
  filters: { ma_alignment: '20>120' | '5>20>120' | null; min_amount: number | null }
  cleaning: {
    mode: CleaningMode
    policy: 'kronos_adapted_v1'
    price_jump_threshold: number | null
    max_illiquid_bars: number | null
    max_stagnant_bars: number | null
    min_segment_bars: number | null
  }
}

export interface ChartIndicatorsConfig {
  preset: string
  moving_averages: {
    window: number
    color: string
    line_width: number
    chart: boolean
    feature: boolean
  }[]
  volume: { chart: boolean; feature: boolean }
}

// Supabase training_presets.preset 전체 JSON (PreprocessPreset)
export interface PresetJson extends PreviewParams {
  name: string
  chart_indicators?: ChartIndicatorsConfig
}

export interface PresetRow {
  id: number
  name: string
  version: number
  schema_version: number
  preset: PresetJson
  archived_at: string | null
  created_at: string
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface JobRow {
  id: number
  kind: string
  status: JobStatus
  completed_items: number
  total_items: number
  error: string | null
  result: Record<string, unknown> | null
}

export type DatasetStatus = 'building' | 'ready' | 'failed'

export interface DatasetRow {
  id: number
  name: string
  preset_id: number
  timeframe: string
  status: DatasetStatus
  feature_columns: string[]
  sample_count: number
  symbol_count: number
  class_counts: Record<string, number>
  failure_message: string | null
  created_at: string
  completed_at: string | null
  preset_snapshot: {
    preset_name?: string
    preset_version?: number
    split?: { method: string; seed: number; ratios: Record<string, number> }
  }
}

export interface DatasetSymbolRow {
  symbol: string
  split: 'train' | 'validation' | 'test' | null
  status: 'pending' | 'running' | 'ready' | 'failed'
  sample_count: number
  class_counts: Record<string, number>
  error: string | null
}

export interface DatasetDetail extends DatasetRow {
  symbols: DatasetSymbolRow[]
  shards: {
    symbol: string
    shard_index: number
    size_bytes: number
    row_count: number
    sha256: string
  }[]
}

export interface BatchStartResponse {
  job_id: number
  dataset_id: number
}

export interface SampleListItem {
  index: number
  symbol: string
  split: 'train' | 'validation' | 'test' | null
  label: 0 | 1 | 2
  kind: 'low' | 'high'
  start_time: string
  end_time: string
  length: number
}

export interface SampleListResponse {
  dataset_id: number
  total: number
  offset: number
  limit: number
  label: number | null
  items: SampleListItem[]
}

export interface SampleDetail extends SampleListItem {
  feature_columns: string[]
  features: number[][]
}

export type DiagnosticTarget = 'raw_cache' | 'preset' | 'dataset'
export type DiagnosticStatus = 'passed' | 'warning' | 'failed'

export interface DiagnosticCheck {
  id: string
  symbol?: string
  status: DiagnosticStatus
  message: string
  data?: Record<string, unknown>
}

export interface DiagnosticReportRow {
  id: number
  target_type: DiagnosticTarget
  preset_id: number | null
  dataset_id: number | null
  status: DiagnosticStatus
  summary: { passed: number; warning: number; failed: number; checks: number }
  created_at: string
}

export interface DiagnosticReportDetail extends DiagnosticReportRow {
  report: { checks: DiagnosticCheck[]; input: Record<string, unknown> }
}

export interface CleanupReport {
  stale_jobs_cancelled: number[]
  stale_datasets_failed: number[]
  orphan_objects_removed: string[]
}

export type TimeframeCode =
  | 'day'
  | 'min1'
  | 'min3'
  | 'min5'
  | 'min10'
  | 'min15'
  | 'min30'
  | 'min45'
  | 'min60'
  | 'tick1'
  | 'tick3'
  | 'tick5'
  | 'tick10'
  | 'tick30'

export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'content-type': 'application/json', ...init?.headers },
    ...init,
  })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${body || path}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  dummyChart: () => fetchJson<ChartResponse>('/api/chart/dummy'),
  watchlist: () => fetchJson<WatchItem[]>('/api/watchlist'),
  symbolSearch: (query: string, signal?: AbortSignal) => {
    const params = new URLSearchParams({ q: query, limit: '10' })
    return fetchJson<SymbolSuggestion[]>(`/api/symbols/search?${params}`, { signal })
  },
  addWatchItem: (item: WatchItem) =>
    fetchJson<WatchItem[]>('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify(item),
    }),
  removeWatchItem: (symbol: string) =>
    fetchJson<WatchItem[]>(`/api/watchlist/${encodeURIComponent(symbol)}`, {
      method: 'DELETE',
    }),
  ingest: (symbols: string[], timeframe: TimeframeCode, options: IngestOptions = {}) =>
    fetchJson<IngestResponse>('/api/ingest', {
      method: 'POST',
      body: JSON.stringify({
        symbols,
        timeframe,
        start: options.start || null,
        end: options.end || null,
      }),
    }),
  ingestStatus: (symbols: string[], timeframe: TimeframeCode) => {
    const params = new URLSearchParams({
      symbols: symbols.join(','),
      timeframe,
    })
    return fetchJson<Record<string, CacheStatus | null>>(`/api/ingest/status?${params}`)
  },
  chart: (
    symbol: string,
    timeframe: TimeframeCode,
    maWindows: number[] = [],
    options: ChartOptions = {},
  ) => {
    const params = new URLSearchParams({ timeframe })
    if (maWindows.length > 0) params.set('ma', maWindows.join(','))
    if (options.limit) params.set('limit', String(options.limit))
    if (options.before !== undefined) params.set('before', String(options.before))
    return fetchJson<ChartResponse>(`/api/chart/${encodeURIComponent(symbol)}?${params}`)
  },
  preprocessPreview: (symbol: string, params: PreviewParams, signal?: AbortSignal) =>
    fetchJson<PreviewResponse>('/api/preprocess/preview', {
      method: 'POST',
      body: JSON.stringify({ symbol, params }),
      signal,
    }),
  presets: (includeArchived = false) =>
    fetchJson<PresetRow[]>(`/api/presets?include_archived=${includeArchived}`),
  createPreset: (preset: PresetJson) =>
    fetchJson<PresetRow>('/api/presets', {
      method: 'POST',
      body: JSON.stringify({ preset }),
    }),
  createPresetVersion: (presetId: number, preset: PresetJson) =>
    fetchJson<PresetRow>(`/api/presets/${presetId}`, {
      method: 'PUT',
      body: JSON.stringify({ preset }),
    }),
  archivePreset: (presetId: number) =>
    fetchJson<PresetRow>(`/api/presets/${presetId}`, { method: 'DELETE' }),
  preprocessBatch: (presetId: number, datasetName: string, symbols: string[]) =>
    fetchJson<BatchStartResponse>('/api/preprocess/batch', {
      method: 'POST',
      body: JSON.stringify({
        preset_id: presetId,
        dataset_name: datasetName,
        symbols,
      }),
    }),
  job: (jobId: number) => fetchJson<JobRow>(`/api/jobs/${jobId}`),
  cancelJob: (jobId: number) =>
    fetchJson<JobRow>(`/api/jobs/${jobId}/cancel`, { method: 'POST' }),
  datasets: () => fetchJson<DatasetRow[]>('/api/datasets'),
  dataset: (datasetId: number) => fetchJson<DatasetDetail>(`/api/datasets/${datasetId}`),
  deleteDataset: (datasetId: number) =>
    fetchJson<{ job_id: number; deleted_objects: number }>(`/api/datasets/${datasetId}`, {
      method: 'DELETE',
    }),
  datasetSamples: (
    datasetId: number,
    options: { label?: number | null; offset?: number; limit?: number } = {},
  ) => {
    const params = new URLSearchParams()
    if (options.label !== undefined && options.label !== null)
      params.set('label', String(options.label))
    if (options.offset !== undefined) params.set('offset', String(options.offset))
    if (options.limit !== undefined) params.set('limit', String(options.limit))
    return fetchJson<SampleListResponse>(`/api/datasets/${datasetId}/samples?${params}`)
  },
  datasetSample: (datasetId: number, sampleIndex: number) =>
    fetchJson<SampleDetail>(`/api/datasets/${datasetId}/samples/${sampleIndex}`),
  cleanup: () => fetchJson<CleanupReport>('/api/datasets/cleanup', { method: 'POST' }),
  diagnosticReports: (targetType?: DiagnosticTarget) =>
    fetchJson<DiagnosticReportRow[]>(
      `/api/diagnostics${targetType ? `?target_type=${targetType}` : ''}`,
    ),
  diagnosticReport: (reportId: number) =>
    fetchJson<DiagnosticReportDetail>(`/api/diagnostics/${reportId}`),
  diagnoseCache: (symbols: string[], timeframe: TimeframeCode) =>
    fetchJson<DiagnosticReportDetail>('/api/diagnostics/cache', {
      method: 'POST',
      body: JSON.stringify({ symbols, timeframe }),
    }),
  diagnosePreview: (presetId: number, symbols: string[]) =>
    fetchJson<DiagnosticReportDetail>('/api/diagnostics/preview', {
      method: 'POST',
      body: JSON.stringify({ preset_id: presetId, symbols }),
    }),
  diagnoseDataset: (datasetId: number) =>
    fetchJson<DiagnosticReportDetail>(`/api/diagnostics/datasets/${datasetId}`, {
      method: 'POST',
    }),
}
