import {
  fetchJson,
  type ChartOptions,
  type ChartResponse,
  type InstrumentRegion,
  type SamplePairing,
  type TimeframeCode,
} from './client'

// docs/08_m5_implementation_plan.md §6의 확정된 HTTP/WebSocket 계약.

export type LiveConnectionStatus =
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'stale'
  | 'closed'

export interface LiveConnection {
  status: LiveConnectionStatus
  message: string | null
  last_tick_at: string | null
  last_heartbeat_at: string | null
  market_state: string | null
}

/** 활성 deployment 메타데이터. artifact object path는 계약상 응답에서 제외된다. */
export interface LiveDeployment {
  id: number
  run_id: number
  run_name: string
  artifact_id: number
  dataset_id: number
  dataset_name: string
  model: string
  timeframe: string
  feature_columns: string[]
  pairing_rule: SamplePairing
  status: 'activating' | 'active' | 'failed'
  activated_at: string | null
}

export type LiveSubscriptionStatus = 'pending' | 'subscribed' | 'error'
export type LiveInferenceStatus = 'no_model' | 'warmup' | 'ready'

export interface LiveSubscription {
  symbol: string
  name: string | null
  region: InstrumentRegion
  exchange: string
  status: LiveSubscriptionStatus
  inference_status: LiveInferenceStatus
  error: string | null
  last_tick_at: string | null
}

export interface LiveStateResponse {
  connection: LiveConnection
  deployment: LiveDeployment | null
  prediction_threshold: number
  manual_anchors: ManualAnchor[]
  subscriptions: LiveSubscription[]
  counters: Record<string, number>
}

export interface ManualAnchor {
  symbol: string
  timeframe: string
  time: string | number
}

/** 실시간 집계 봉. 차트 API Candle과 달리 거래량을 함께 싣는다. */
export interface LiveCandle {
  time: string | number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface LiveFractalMarker {
  time: string | number
  kind: 'low' | 'high'
  label: 0 | 1 | 2
}

export interface LiveHistoryResponse extends ChartResponse {
  fractal_markers: LiveFractalMarker[]
}

export interface CandleEventData {
  symbol: string
  timeframe: string
  candle: LiveCandle
  provisional: boolean
}

export interface CandidateWindow {
  pairing_rule: SamplePairing
  anchor_position: number
  anchor_time: string | number
  anchor_kind: 'low' | 'high' | 'manual'
  anchor_source: 'calculated' | 'prediction' | 'manual'
  anchor_confidence: number | null
  start: string | number
  end: string | number
  shared_window: boolean
}

export interface PredictionEventData {
  symbol: string
  timeframe: string
  time: string | number
  /** 클래스 순서 고정: [0=저점, 1=고점, 2=무시] */
  scores: [number, number, number]
  selected_class: 0 | 1 | 2
  candidate_windows: CandidateWindow[]
  deployment_id: number
}

export interface WarmupEventData {
  symbol: string
  required_bars: number
  available_bars: number
  reason: string
}

export interface ConnectionEventData {
  status: LiveConnectionStatus
  message: string | null
}

export type SubscriptionEventData = LiveSubscription

export interface HeartbeatEventData {
  server_time: string
  market_state: string | null
  last_tick_at: string | null
}

export interface ErrorEventData {
  scope: string
  symbol?: string
  recoverable: boolean
  message: string
}

export interface SnapshotEventData {
  connection: LiveConnection
  deployment: LiveDeployment | null
  prediction_threshold: number
  manual_anchors: ManualAnchor[]
  subscriptions: LiveSubscription[]
  counters: Record<string, number>
  latest_candles: CandleEventData[]
  recent_predictions: PredictionEventData[]
}

export type LiveEventType =
  | 'snapshot'
  | 'connection'
  | 'subscription'
  | 'candle_update'
  | 'candle_closed'
  | 'prediction'
  | 'warmup'
  | 'heartbeat'
  | 'error'

export interface LiveEvent {
  type: LiveEventType
  sequence: number
  emitted_at: string
  data: unknown
}

export const liveApi = {
  state: () => fetchJson<LiveStateResponse>('/api/live/state'),
  activateModel: (runId: number, artifactId?: number) =>
    fetchJson<LiveStateResponse>('/api/live/model', {
      method: 'PUT',
      body: JSON.stringify(
        artifactId === undefined
          ? { run_id: runId }
          : { run_id: runId, artifact_id: artifactId },
      ),
    }),
  deactivateModel: () =>
    fetchJson<LiveStateResponse>('/api/live/model', { method: 'DELETE' }),
  setPredictionThreshold: (threshold: number) =>
    fetchJson<LiveStateResponse>('/api/live/prediction-threshold', {
      method: 'PUT',
      body: JSON.stringify({ threshold }),
    }),
  setManualAnchor: (symbol: string, timeframe: TimeframeCode, time: string | number) =>
    fetchJson<LiveStateResponse>(`/api/live/anchors/${encodeURIComponent(symbol)}`, {
      method: 'PUT',
      body: JSON.stringify({ timeframe, time }),
    }),
  clearManualAnchor: (symbol: string) =>
    fetchJson<LiveStateResponse>(`/api/live/anchors/${encodeURIComponent(symbol)}`, {
      method: 'DELETE',
    }),
  subscriptions: () => fetchJson<LiveSubscription[]>('/api/live/subscriptions'),
  subscribe: (instrument: {
    symbol: string
    name: string
    region: InstrumentRegion
    exchange: string
  }) =>
    fetchJson<LiveSubscription[]>('/api/live/subscriptions', {
      method: 'POST',
      body: JSON.stringify(instrument),
    }),
  unsubscribe: (symbol: string) =>
    fetchJson<LiveSubscription[]>(
      `/api/live/subscriptions/${encodeURIComponent(symbol)}`,
      { method: 'DELETE' },
    ),
  history: (
    symbol: string,
    timeframe: TimeframeCode,
    maWindows: number[] = [],
    options: ChartOptions = {},
  ) => {
    const params = new URLSearchParams({ timeframe })
    if (maWindows.length > 0) params.set('ma', maWindows.join(','))
    if (options.before !== undefined) params.set('before', String(options.before))
    return fetchJson<LiveHistoryResponse>(
      `/api/live/history/${encodeURIComponent(symbol)}?${params}`,
    )
  },
  socketUrl: () =>
    `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/live`,
}
