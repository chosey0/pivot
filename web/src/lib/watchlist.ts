import type { TimeframeCode, WatchItem } from '../api/client'

export function watchItemKey(item: WatchItem) {
  return [
    item.region,
    item.exchange,
    item.symbol,
    item.timeframe,
    item.start ?? '',
    item.end ?? '',
  ].join(':')
}

export function datasetSourceKey(
  item: Pick<WatchItem, 'region' | 'exchange' | 'symbol' | 'timeframe' | 'start' | 'end'>,
) {
  return [
    item.region,
    item.exchange,
    item.symbol,
    item.timeframe,
    item.start ?? '',
    item.end ?? '',
  ].join('|')
}

export function timeframeLabel(timeframe: TimeframeCode) {
  if (timeframe === 'day') return '1day'
  if (timeframe.startsWith('min')) return `${timeframe.slice(3)}min`
  return `${timeframe.slice(4)}tick`
}

export function watchItemsForTimeframe(items: WatchItem[], timeframe: TimeframeCode) {
  const unique = new Map<string, WatchItem>()
  for (const item of items) {
    if (item.timeframe !== timeframe) continue
    const key = `${item.region}:${item.exchange}:${item.symbol}`
    if (!unique.has(key)) unique.set(key, item)
  }
  return [...unique.values()]
}
