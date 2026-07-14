import type { TimeframeCode, WatchItem } from '../api/client'

export function watchItemsForTimeframe(items: WatchItem[], timeframe: TimeframeCode) {
  const unique = new Map<string, WatchItem>()
  for (const item of items) {
    if (item.timeframe !== timeframe) continue
    const key = `${item.region}:${item.exchange}:${item.symbol}`
    if (!unique.has(key)) unique.set(key, item)
  }
  return [...unique.values()]
}
