import type { ChartResponse } from '../api/client'

function compareTimes(a: string | number, b: string | number): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b))
}

export function mergeChartPages(current: ChartResponse, older: ChartResponse): ChartResponse {
  const candles = [...new Map(
    [...older.candles, ...current.candles].map((row) => [row.time, row]),
  ).values()].sort((a, b) => compareTimes(a.time, b.time))
  const times = new Set(candles.map((row) => row.time))
  const volumes = [...new Map(
    [...older.volumes, ...current.volumes]
      .filter((row) => times.has(row.time))
      .map((row) => [row.time, row]),
  ).values()].sort((a, b) => compareTimes(a.time, b.time))
  const ma = Object.fromEntries(
    [...new Set([...Object.keys(older.ma), ...Object.keys(current.ma)])].map((window) => [
      window,
      [...new Map(
        [...(older.ma[window] ?? []), ...(current.ma[window] ?? [])]
          .filter((row) => times.has(row.time))
          .map((row) => [row.time, row]),
      ).values()].sort((a, b) => compareTimes(a.time, b.time)),
    ]),
  )
  return {
    ...current,
    candles,
    volumes,
    ma,
    has_more: older.has_more,
    next_before: older.next_before,
  }
}
