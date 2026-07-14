export type PriceDecimals = 0 | 2

export function formatChartPrice(value: number, decimals: PriceDecimals) {
  return value.toLocaleString('ko-KR', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

export function chartPriceFormat(decimals: PriceDecimals) {
  return {
    type: 'price' as const,
    precision: decimals,
    minMove: decimals === 2 ? 0.01 : 1,
  }
}
