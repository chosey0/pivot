// 표시 포맷터 — KRW 가격(소수점 없음, 천 단위 콤마), 등락률, 일시
const KRW_FORMATTER = new Intl.NumberFormat('ko-KR', {
  maximumFractionDigits: 0,
})
const USD_FORMATTER = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
})

export function formatDateTime(value?: string) {
  if (!value) return '-'
  return value.replace('T', ' ').slice(0, 19)
}

export function formatPrice(value: number, currency: 'KRW' | 'USD' = 'KRW') {
  return currency === 'USD' ? `$${USD_FORMATTER.format(value)}` : `${KRW_FORMATTER.format(value)}원`
}

export function percentChange(value: number, previousClose: number | null) {
  if (!previousClose) return null
  return ((value - previousClose) / previousClose) * 100
}

export function formatPercent(value: number) {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

export function changeTone(value: number | null) {
  if (value === null) return 'neutral'
  if (value > 0) return 'up'
  if (value < 0) return 'down'
  return 'neutral'
}
