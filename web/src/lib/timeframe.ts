// 타임프레임 공용 상수/변환 — 허용 단위는 Kiwoom SDK 기준 (docs/03 §2)
import type { TimeframeCode } from '../api/client'

export const MINUTE_UNITS = [1, 3, 5, 10, 15, 30, 45, 60]
export const TICK_UNITS = [1, 3, 5, 10, 30]

// 분/틱봉은 전체 로드 대신 최근 구간만 로드 (has_more로 과거 추가 로딩)
const INTRADAY_CHART_LIMIT = 5000

export type TimeframeKind = 'day' | 'minute' | 'tick'

export function toTimeframeCode(kind: TimeframeKind, unit: number): TimeframeCode {
  if (kind === 'day') return 'day'
  return `${kind === 'minute' ? 'min' : 'tick'}${unit}` as TimeframeCode
}

export function fromTimeframeCode(timeframe: TimeframeCode): {
  type: TimeframeKind
  unit: number
} {
  if (timeframe === 'day') return { type: 'day', unit: 1 }
  if (timeframe.startsWith('min')) {
    return { type: 'minute', unit: Number(timeframe.slice(3)) }
  }
  return { type: 'tick', unit: Number(timeframe.slice(4)) }
}

export function chartLimitFor(timeframe: TimeframeCode) {
  return timeframe === 'day' ? undefined : INTRADAY_CHART_LIMIT
}
