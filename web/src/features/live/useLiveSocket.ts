import { useCallback, useEffect, useReducer, useRef } from 'react'
import {
  liveApi,
  type CandleEventData,
  type ConnectionEventData,
  type ErrorEventData,
  type HeartbeatEventData,
  type LiveCandle,
  type LiveConnection,
  type LiveDeployment,
  type LiveEvent,
  type LiveStateResponse,
  type LiveSubscription,
  type PredictionEventData,
  type SnapshotEventData,
  type SubscriptionEventData,
  type WarmupEventData,
} from '../../api/live'

/** 브라우저 ↔ FastAPI WebSocket 상태 (Kiwoom 연결 상태와 별개) */
export type SocketStatus = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface SymbolCandles {
  /** 마감 봉 — time 기준 unique·오름차순 유지 */
  closed: LiveCandle[]
  /** 아직 마감하지 않은 잠정 봉 (candle_update) */
  provisional: LiveCandle | null
}

export interface LiveErrorInfo extends ErrorEventData {
  at: string
}

export interface LiveSocketState {
  connection: LiveConnection
  deployment: LiveDeployment | null
  subscriptions: LiveSubscription[]
  candles: Record<string, SymbolCandles>
  predictions: PredictionEventData[]
  warmups: Record<string, WarmupEventData>
  lastError: LiveErrorInfo | null
  /** snapshot 수신마다 증가 — 재연결 후 과거 차트 재조회 트리거 */
  snapshotNonce: number
  hasSnapshot: boolean
  ignoredEvents: number
}

export function liveCandleKey(symbol: string, timeframe: string): string {
  return `${symbol}:${timeframe}`
}

const EMPTY_CONNECTION: LiveConnection = {
  status: 'connecting',
  message: null,
  last_tick_at: null,
  last_heartbeat_at: null,
  market_state: null,
}

const INITIAL_STATE: LiveSocketState = {
  connection: EMPTY_CONNECTION,
  deployment: null,
  subscriptions: [],
  candles: {},
  predictions: [],
  warmups: {},
  lastError: null,
  snapshotNonce: 0,
  hasSnapshot: false,
  ignoredEvents: 0,
}

const MAX_PREDICTIONS = 50
const MAX_LIVE_CANDLES = 500
const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS = 15000

function compareTimes(a: string | number, b: string | number): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b))
}

function upsertClosedCandle(rows: LiveCandle[], next: LiveCandle): LiveCandle[] {
  const merged = rows.filter((row) => compareTimes(row.time, next.time) !== 0)
  merged.push(next)
  merged.sort((a, b) => compareTimes(a.time, b.time))
  return merged.slice(-MAX_LIVE_CANDLES)
}

function applyCandleEvent(
  candles: Record<string, SymbolCandles>,
  data: CandleEventData,
  closed: boolean,
): Record<string, SymbolCandles> {
  const key = liveCandleKey(data.symbol, data.timeframe)
  const current = candles[key] ?? { closed: [], provisional: null }
  if (closed) {
    const nextClosed = upsertClosedCandle(current.closed, data.candle)
    const provisional =
      current.provisional && compareTimes(current.provisional.time, data.candle.time) > 0
        ? current.provisional
        : null
    return { ...candles, [key]: { closed: nextClosed, provisional } }
  }
  // 이미 마감된 시각보다 오래된 잠정 봉은 차트를 되감지 않도록 무시한다
  const lastClosed = current.closed[current.closed.length - 1]
  if (lastClosed && compareTimes(data.candle.time, lastClosed.time) < 0) return candles
  return { ...candles, [key]: { ...current, provisional: data.candle } }
}

function upsertSubscription(
  rows: LiveSubscription[],
  data: SubscriptionEventData,
): LiveSubscription[] {
  const existing = rows.find((row) => row.symbol === data.symbol)
  if (!existing) {
    return [
      ...rows,
      {
        symbol: data.symbol,
        name: null,
        status: data.status,
        inference_status: 'no_model',
        error: data.error,
        last_tick_at: null,
      },
    ]
  }
  return rows.map((row) =>
    row.symbol === data.symbol ? { ...row, status: data.status, error: data.error } : row,
  )
}

function setInference(
  rows: LiveSubscription[],
  symbol: string,
  inference: LiveSubscription['inference_status'],
): LiveSubscription[] {
  return rows.map((row) =>
    row.symbol === symbol ? { ...row, inference_status: inference } : row,
  )
}

type Action =
  | { type: 'socket_event'; event: LiveEvent }
  | { type: 'ignored' }
  | { type: 'apply_state'; state: LiveStateResponse }
  | { type: 'apply_subscriptions'; rows: LiveSubscription[] }
  | { type: 'dismiss_error' }

function applyEvent(state: LiveSocketState, event: LiveEvent): LiveSocketState {
  switch (event.type) {
    case 'snapshot': {
      const data = event.data as SnapshotEventData
      let candles: Record<string, SymbolCandles> = {}
      for (const entry of data.latest_candles ?? []) {
        candles = applyCandleEvent(candles, entry, !entry.provisional)
      }
      return {
        ...state,
        connection: { ...EMPTY_CONNECTION, ...data.connection },
        deployment: data.deployment ?? null,
        subscriptions: data.subscriptions ?? [],
        candles,
        predictions: (data.recent_predictions ?? []).slice(-MAX_PREDICTIONS),
        warmups: {},
        snapshotNonce: state.snapshotNonce + 1,
        hasSnapshot: true,
      }
    }
    case 'connection': {
      const data = event.data as ConnectionEventData
      return {
        ...state,
        connection: { ...state.connection, status: data.status, message: data.message },
      }
    }
    case 'subscription': {
      const data = event.data as SubscriptionEventData
      return { ...state, subscriptions: upsertSubscription(state.subscriptions, data) }
    }
    case 'candle_update': {
      const data = event.data as CandleEventData
      return { ...state, candles: applyCandleEvent(state.candles, data, false) }
    }
    case 'candle_closed': {
      const data = event.data as CandleEventData
      return { ...state, candles: applyCandleEvent(state.candles, data, true) }
    }
    case 'prediction': {
      const data = event.data as PredictionEventData
      const warmups = { ...state.warmups }
      delete warmups[data.symbol]
      return {
        ...state,
        predictions: [...state.predictions, data].slice(-MAX_PREDICTIONS),
        warmups,
        subscriptions: setInference(state.subscriptions, data.symbol, 'ready'),
      }
    }
    case 'warmup': {
      const data = event.data as WarmupEventData
      return {
        ...state,
        warmups: { ...state.warmups, [data.symbol]: data },
        subscriptions: setInference(state.subscriptions, data.symbol, 'warmup'),
      }
    }
    case 'heartbeat': {
      const data = event.data as HeartbeatEventData
      return {
        ...state,
        connection: {
          ...state.connection,
          last_heartbeat_at: data.server_time,
          market_state: data.market_state,
          last_tick_at: data.last_tick_at ?? state.connection.last_tick_at,
        },
      }
    }
    case 'error': {
      const data = event.data as ErrorEventData
      return { ...state, lastError: { ...data, at: event.emitted_at } }
    }
    default:
      return { ...state, ignoredEvents: state.ignoredEvents + 1 }
  }
}

function reducer(state: LiveSocketState, action: Action): LiveSocketState {
  switch (action.type) {
    case 'socket_event':
      return applyEvent(state, action.event)
    case 'ignored':
      return { ...state, ignoredEvents: state.ignoredEvents + 1 }
    case 'apply_state':
      return {
        ...state,
        connection: { ...state.connection, ...action.state.connection },
        deployment: action.state.deployment,
        subscriptions: action.state.subscriptions,
      }
    case 'apply_subscriptions': {
      const symbols = new Set(action.rows.map((row) => row.symbol))
      const warmups = Object.fromEntries(
        Object.entries(state.warmups).filter(([symbol]) => symbols.has(symbol)),
      )
      return { ...state, subscriptions: action.rows, warmups }
    }
    case 'dismiss_error':
      return { ...state, lastError: null }
  }
}

/**
 * /ws/live 구독 훅. 첫 snapshot을 신뢰 상태로 삼고 이후 이벤트는 sequence 오름차순만
 * 적용한다 (docs/08 §6.2). 재연결 시 이전 delta를 재생하지 않고 새 snapshot으로 교체하며,
 * 브라우저 WS 단절은 제한 백오프(1s→최대 15s)로 재접속한다.
 */
export function useLiveSocket() {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE)
  const [socketStatus, setSocketStatus] = useReducer(
    (_: SocketStatus, next: SocketStatus) => next,
    'connecting',
  )
  const lastSequenceRef = useRef<number | null>(null)
  const hasSnapshotRef = useRef(false)

  useEffect(() => {
    let stale = false
    let socket: WebSocket | null = null
    let retryTimer: number | undefined
    let attempts = 0

    const connect = () => {
      if (stale) return
      // 재연결 후에는 새 snapshot이 올 때까지 delta를 적용하지 않는다
      hasSnapshotRef.current = false
      lastSequenceRef.current = null
      socket = new WebSocket(liveApi.socketUrl())
      socket.onopen = () => {
        if (!stale) setSocketStatus('open')
      }
      socket.onmessage = (message: MessageEvent) => {
        if (stale) return
        let event: LiveEvent
        try {
          event = JSON.parse(String(message.data)) as LiveEvent
        } catch {
          dispatch({ type: 'ignored' })
          return
        }
        if (!event || typeof event.type !== 'string' || typeof event.sequence !== 'number') {
          dispatch({ type: 'ignored' })
          return
        }
        if (event.type === 'snapshot') {
          hasSnapshotRef.current = true
          lastSequenceRef.current = event.sequence
          attempts = 0
          dispatch({ type: 'socket_event', event })
          return
        }
        // snapshot 이전 delta, 중복/역행 sequence는 적용하지 않는다
        if (!hasSnapshotRef.current) {
          dispatch({ type: 'ignored' })
          return
        }
        const last = lastSequenceRef.current
        if (last !== null && event.sequence <= last) {
          dispatch({ type: 'ignored' })
          return
        }
        lastSequenceRef.current = event.sequence
        dispatch({ type: 'socket_event', event })
      }
      socket.onclose = () => {
        if (stale) return
        setSocketStatus('reconnecting')
        attempts += 1
        const delay = Math.min(RECONNECT_BASE_MS * 2 ** (attempts - 1), RECONNECT_MAX_MS)
        window.clearTimeout(retryTimer)
        retryTimer = window.setTimeout(connect, delay)
      }
    }

    setSocketStatus('connecting')
    retryTimer = window.setTimeout(connect, 0)

    return () => {
      stale = true
      window.clearTimeout(retryTimer)
      if (socket) {
        socket.onclose = null
        socket.close()
      }
    }
  }, [])

  const applyState = useCallback(
    (next: LiveStateResponse) => dispatch({ type: 'apply_state', state: next }),
    [],
  )
  const applySubscriptions = useCallback(
    (rows: LiveSubscription[]) => dispatch({ type: 'apply_subscriptions', rows }),
    [],
  )
  const dismissError = useCallback(() => dispatch({ type: 'dismiss_error' }), [])

  return { state, socketStatus, applyState, applySubscriptions, dismissError }
}
