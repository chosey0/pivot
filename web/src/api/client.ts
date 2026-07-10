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
  confirmation_lag: number
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
  fractal: { n: number }
  ma_windows: number[]
  features: string[]
  labeling: { mode: 'cls3' | 'cls2_drop'; ignore_rule: 'ma20<ma120' | 'none' }
  filters: { ma_alignment: '20>120' | '5>20>120' | null; min_amount: number | null }
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
  datasets: () => fetchJson<DatasetRow[]>('/api/datasets'),
  dataset: (datasetId: number) => fetchJson<DatasetDetail>(`/api/datasets/${datasetId}`),
}
