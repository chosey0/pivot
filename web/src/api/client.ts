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
}

export interface WatchItem {
  symbol: string
  name: string
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
  chart: (symbol: string, timeframe: TimeframeCode, maWindows: number[] = []) => {
    const params = new URLSearchParams({ timeframe })
    if (maWindows.length > 0) params.set('ma', maWindows.join(','))
    return fetchJson<ChartResponse>(`/api/chart/${encodeURIComponent(symbol)}?${params}`)
  },
}
