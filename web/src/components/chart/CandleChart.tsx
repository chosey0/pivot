import { useEffect, useRef } from 'react'
import {
  createChart,
  createSeriesMarkers,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type IPrimitivePaneRenderer,
  type IPrimitivePaneView,
  type ISeriesMarkersPluginApi,
  type ISeriesPrimitive,
  type SeriesAttachedParameter,
  type SeriesMarker,
  type CandlestickData,
  type HistogramData,
  type LineData,
  type Time,
} from 'lightweight-charts'
import type { Candle, LinePoint, VolumePoint } from '../../api/client'

// 국내 관례: 상승 빨강 / 하락 파랑
const UP_COLOR = '#e5484d'
const DOWN_COLOR = '#3b82f6'
export interface VisibleIndicators {
  movingAverages: { window: string; color: string; lineWidth: 1 | 2 | 3 | 4 }[]
  volume: boolean
}

export interface OhlcPoint {
  time: string | number
  open: number
  high: number
  low: number
  close: number
}

// 프랙탈 라벨 마커. label 규약: 0=저점, 1=고점, 2=무시 (docs/04 §5)
export interface ChartMarker {
  time: string | number
  kind: 'low' | 'high'
  label: 0 | 1 | 2
  source?: 'calculated' | 'prediction'
}

export interface TimeRange {
  from: string | number
  to: string | number
}

interface Props {
  candles: Candle[]
  volumes?: VolumePoint[]
  ma?: Record<string, LinePoint[]>
  visibleIndicators?: VisibleIndicators
  markers?: ChartMarker[]
  highlightRange?: TimeRange | null
  onOhlcChange?: (point: OhlcPoint) => void
  onTimeClick?: (time: string | number) => void
  onLoadMoreOlder?: () => void
  canLoadMoreOlder?: boolean
  isLoadingOlder?: boolean
  fitContentKey?: string
}

/** lightweight-charts 내부 시간값(BusinessDay 객체 등)을 API 시간값으로 되돌린다. */
function timeToKey(time: Time): string | number {
  if (typeof time === 'object') {
    const pad = (value: number) => String(value).padStart(2, '0')
    return `${time.year}-${pad(time.month)}-${pad(time.day)}`
  }
  return time
}

/** 크로스헤어 시간 라벨: 일봉 yyyy-mm-dd, 분/틱봉(unix 초)은 초 단위까지. */
function formatTimeLabel(time: Time): string {
  if (typeof time === 'number') {
    // 백엔드가 naive(KST 벽시계) 시각을 UTC epoch로 직렬화하므로 UTC getter로 복원
    const date = new Date(time * 1000)
    const pad = (value: number) => String(value).padStart(2, '0')
    return (
      `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
      ` ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`
    )
  }
  return String(timeToKey(time))
}

/** 원화 가격축: 소수점 없이 천 단위 구분 표기 (예: 1,000,000). */
function formatKrwPrice(price: number): string {
  return Math.round(price).toLocaleString('ko-KR')
}

function compareTimes(a: string | number, b: string | number): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b))
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function uniqueSortedByTime<T extends { time: string | number }>(rows: T[]): T[] {
  const byTime = new Map<string | number, T>()
  for (const row of rows) {
    byTime.set(row.time, row)
  }
  return [...byTime.values()].sort((a, b) => compareTimes(a.time, b.time))
}

function toCandlestickData(candles: Candle[]): CandlestickData<Time>[] {
  return uniqueSortedByTime(
    candles.filter(
      (candle) =>
        isFiniteNumber(candle.open) &&
        isFiniteNumber(candle.high) &&
        isFiniteNumber(candle.low) &&
        isFiniteNumber(candle.close),
    ),
  ).map((candle) => ({
    time: candle.time as Time,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
  }))
}

function toLineData(points: LinePoint[], validTimes: Set<string | number>): LineData<Time>[] {
  return uniqueSortedByTime(
    points.filter((point) => validTimes.has(point.time) && isFiniteNumber(point.value)),
  ).map((point) => ({
    time: point.time as Time,
    value: point.value,
  }))
}

function toSeriesMarker(marker: ChartMarker): SeriesMarker<Time> {
  const time = marker.time as Time
  if (marker.source === 'prediction') {
    if (marker.label === 0) {
      return { time, position: 'belowBar', shape: 'circle', color: '#00897b', text: '예측 L' }
    }
    if (marker.label === 1) {
      return { time, position: 'aboveBar', shape: 'circle', color: '#d32f2f', text: '예측 H' }
    }
    return { time, position: 'aboveBar', shape: 'square', color: '#757575', text: '예측 -' }
  }
  if (marker.label === 0) {
    return { time, position: 'belowBar', shape: 'arrowUp', color: '#26a69a', text: 'L' }
  }
  if (marker.label === 1) {
    return { time, position: 'aboveBar', shape: 'arrowDown', color: '#ef5350', text: 'H' }
  }
  return {
    time,
    position: marker.kind === 'low' ? 'belowBar' : 'aboveBar',
    shape: 'circle',
    color: '#9e9e9e',
  }
}

/** 샘플 입력 윈도우 하이라이트 — v5 series primitive로 반투명 배경을 그린다 (docs/04 §5). */
class RangeHighlightPrimitive implements ISeriesPrimitive<Time> {
  private chart: IChartApi | null = null
  private requestUpdate: (() => void) | null = null
  private range: TimeRange | null = null

  attached(param: SeriesAttachedParameter<Time>) {
    this.chart = param.chart
    this.requestUpdate = param.requestUpdate
  }

  detached() {
    this.chart = null
    this.requestUpdate = null
  }

  setRange(range: TimeRange | null) {
    this.range = range
    this.requestUpdate?.()
  }

  paneViews(): readonly IPrimitivePaneView[] {
    const draw: IPrimitivePaneRenderer['draw'] = (target) => {
      const { chart, range } = this
      if (!chart || !range) return
      const timeScale = chart.timeScale()
      const from = timeScale.timeToCoordinate(range.from as Time)
      const to = timeScale.timeToCoordinate(range.to as Time)
      // 양 끝이 모두 화면 밖이면 (전부 가림/전부 벗어남 구분 불가) 그리지 않는다
      if (from === null && to === null) return
      target.useBitmapCoordinateSpace((scope) => {
        const ratio = scope.horizontalPixelRatio
        const left = from !== null ? from * ratio : 0
        const right = to !== null ? to * ratio : scope.bitmapSize.width
        if (right <= left) return
        scope.context.fillStyle = 'rgba(37, 99, 235, 0.14)'
        scope.context.fillRect(left, 0, right - left, scope.bitmapSize.height)
      })
    }
    return [{ renderer: () => ({ draw }) }]
  }
}

export function CandleChart({
  candles,
  volumes = [],
  ma = {},
  visibleIndicators = {
    movingAverages: [
      { window: '20', color: '#5A639C', lineWidth: 2 },
      { window: '120', color: '#A0937D', lineWidth: 2 },
    ],
    volume: true,
  },
  markers = [],
  highlightRange = null,
  onOhlcChange,
  onTimeClick,
  onLoadMoreOlder,
  canLoadMoreOlder = false,
  isLoadingOlder = false,
  fitContentKey = '',
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const maSeriesRef = useRef<Record<string, ISeriesApi<'Line'>>>({})
  const markersApiRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const highlightRef = useRef<RangeHighlightPrimitive | null>(null)
  const onOhlcChangeRef = useRef(onOhlcChange)
  const onTimeClickRef = useRef(onTimeClick)
  const onLoadMoreOlderRef = useRef(onLoadMoreOlder)
  const canLoadMoreOlderRef = useRef(canLoadMoreOlder)
  const isLoadingOlderRef = useRef(isLoadingOlder)
  const dataLengthRef = useRef(0)
  const fitContentKeyRef = useRef<string | null>(null)

  useEffect(() => {
    onOhlcChangeRef.current = onOhlcChange
    onTimeClickRef.current = onTimeClick
    onLoadMoreOlderRef.current = onLoadMoreOlder
    canLoadMoreOlderRef.current = canLoadMoreOlder
    isLoadingOlderRef.current = isLoadingOlder
  }, [onOhlcChange, onTimeClick, onLoadMoreOlder, canLoadMoreOlder, isLoadingOlder])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { color: 'transparent' },
        textColor: '#6b7280',
      },
      grid: {
        vertLines: { color: 'rgba(107, 114, 128, 0.12)' },
        horzLines: { color: 'rgba(107, 114, 128, 0.12)' },
      },
      localization: {
        timeFormatter: formatTimeLabel,
        priceFormatter: formatKrwPrice,
      },
      timeScale: { borderVisible: false },
      rightPriceScale: { borderVisible: false },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: UP_COLOR,
      downColor: DOWN_COLOR,
      borderVisible: false,
      wickUpColor: UP_COLOR,
      wickDownColor: DOWN_COLOR,
    })
    chartRef.current = chart
    seriesRef.current = series
    markersApiRef.current = createSeriesMarkers(series, [])
    const highlight = new RangeHighlightPrimitive()
    series.attachPrimitive(highlight)
    highlightRef.current = highlight
    chart.subscribeCrosshairMove((param) => {
      const point = param.seriesData.get(series)
      if (!point || !('open' in point)) return
      onOhlcChangeRef.current?.({
        time: point.time as string | number,
        open: point.open,
        high: point.high,
        low: point.low,
        close: point.close,
      })
    })
    chart.subscribeClick((param) => {
      if (param.time === undefined) return
      onTimeClickRef.current?.(timeToKey(param.time))
    })
    const rangeHandler = () => {
      const currentRange = chart.timeScale().getVisibleLogicalRange()
      if (!currentRange || !canLoadMoreOlderRef.current || isLoadingOlderRef.current) return
      const barsInfo = series.barsInLogicalRange(currentRange)
      if (barsInfo && barsInfo.barsBefore < 50) {
        onLoadMoreOlderRef.current?.()
      }
    }
    chart.timeScale().subscribeVisibleLogicalRangeChange(rangeHandler)

    return () => {
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(rangeHandler)
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      volumeSeriesRef.current = null
      maSeriesRef.current = {}
      markersApiRef.current = null
      highlightRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!seriesRef.current || !chart) return

    const visibleMa = new Set(visibleIndicators.movingAverages.map((indicator) => indicator.window))
    for (const window of Object.keys(maSeriesRef.current)) {
      if (!visibleMa.has(window)) {
        chart.removeSeries(maSeriesRef.current[window])
        delete maSeriesRef.current[window]
      }
    }

    for (const indicator of visibleIndicators.movingAverages) {
      const { window } = indicator
      if (!ma[window]) continue
      if (!maSeriesRef.current[window]) {
        maSeriesRef.current[window] = chart.addSeries(LineSeries, {
          color: indicator.color,
          lineWidth: indicator.lineWidth,
          priceLineVisible: false,
          lastValueVisible: false,
        })
      } else {
        maSeriesRef.current[window].applyOptions({
          color: indicator.color,
          lineWidth: indicator.lineWidth,
        })
      }
    }

    if (visibleIndicators.volume && !volumeSeriesRef.current) {
      volumeSeriesRef.current = chart.addSeries(HistogramSeries, {
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
        priceLineVisible: false,
        lastValueVisible: false,
      })
    } else if (!visibleIndicators.volume && volumeSeriesRef.current) {
      chart.removeSeries(volumeSeriesRef.current)
      volumeSeriesRef.current = null
    }

    chart.priceScale('right').applyOptions({
      scaleMargins: {
        top: 0.05,
        bottom: visibleIndicators.volume ? 0.25 : 0.05,
      },
    })
    if (visibleIndicators.volume) {
      chart.priceScale('volume').applyOptions({
        scaleMargins: {
          top: 0.8,
          bottom: 0,
        },
      })
    }

    const previousLength = dataLengthRef.current
    const previousRange = chart.timeScale().getVisibleLogicalRange()
    const candleData = toCandlestickData(candles)
    const validTimes = new Set(candleData.map((candle) => timeToKey(candle.time)))
    const candleDirection = new Map(
      candleData.map((candle) => [timeToKey(candle.time), candle.close >= candle.open]),
    )
    const volumeData = uniqueSortedByTime(
      volumes.filter((point) => validTimes.has(point.time) && isFiniteNumber(point.value)),
    ).map((point) => ({
      time: point.time as Time,
      value: point.value,
      color: candleDirection.get(point.time)
        ? 'rgba(229, 72, 77, 0.45)'
        : 'rgba(59, 130, 246, 0.45)',
    })) as HistogramData<Time>[]
    const maDataByWindow = Object.fromEntries(
      Object.keys(maSeriesRef.current).map((window) => [
        window,
        toLineData(ma[window] ?? [], validTimes),
      ]),
    ) as Record<string, LineData<Time>[]>
    const addedBefore = Math.max(candleData.length - previousLength, 0)
    const replacingContent = fitContentKeyRef.current !== fitContentKey

    if (previousLength > 0 && (addedBefore > 0 || replacingContent)) {
      // setData 사이에는 series별 시간축이 잠시 어긋난다. 기존 crosshair/marker가
      // 그 중간 상태를 hit-test하지 않도록 먼저 해제한다.
      chart.clearCrosshairPosition()
      markersApiRef.current?.setMarkers([])
      for (const series of Object.values(maSeriesRef.current)) {
        series.setData([])
      }
    }

    seriesRef.current.setData(candleData)
    volumeSeriesRef.current?.setData(volumeData)
    for (const [window, series] of Object.entries(maSeriesRef.current)) {
      series.setData(maDataByWindow[window] ?? [])
    }
    if (replacingContent) {
      chart.timeScale().fitContent()
      fitContentKeyRef.current = fitContentKey
    } else if (previousRange && addedBefore > 0) {
      chart.timeScale().setVisibleLogicalRange({
        from: previousRange.from + addedBefore,
        to: previousRange.to + addedBefore,
      })
    }
    dataLengthRef.current = candleData.length
  }, [candles, volumes, ma, visibleIndicators, fitContentKey])

  useEffect(() => {
    markersApiRef.current?.setMarkers(markers.map(toSeriesMarker))
  }, [markers, candles])

  useEffect(() => {
    highlightRef.current?.setRange(highlightRange)
    const chart = chartRef.current
    if (!chart || !highlightRange) return

    const timeScale = chart.timeScale()
    const visibleRange = timeScale.getVisibleLogicalRange()
    const highlightFromIndex = timeScale.timeToIndex(highlightRange.from as Time)
    const highlightToIndex = timeScale.timeToIndex(highlightRange.to as Time)
    if (!visibleRange || highlightFromIndex === null || highlightToIndex === null) return

    const highlightFrom = Math.min(Number(highlightFromIndex), Number(highlightToIndex))
    const highlightTo = Math.max(Number(highlightFromIndex), Number(highlightToIndex))
    if (highlightFrom >= visibleRange.from && highlightTo <= visibleRange.to) return

    const visibleWidth = Math.max(visibleRange.to - visibleRange.from, 20)
    const highlightWidth = Math.max(highlightTo - highlightFrom, 1)
    const padding = Math.min(Math.max(visibleWidth * 0.15, 5), Math.max(visibleWidth * 0.3, 5))

    if (highlightWidth + padding * 2 >= visibleWidth) {
      timeScale.setVisibleLogicalRange({
        from: highlightFrom - padding,
        to: highlightTo + padding,
      })
      return
    }

    if (highlightFrom < visibleRange.from) {
      const nextFrom = highlightFrom - padding
      timeScale.setVisibleLogicalRange({
        from: nextFrom,
        to: nextFrom + visibleWidth,
      })
      return
    }

    const nextTo = highlightTo + padding
    timeScale.setVisibleLogicalRange({
      from: nextTo - visibleWidth,
      to: nextTo,
    })
  }, [highlightRange, candles])

  return <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
}
