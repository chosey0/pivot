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

function toSeriesMarker(marker: ChartMarker): SeriesMarker<Time> {
  const time = marker.time as Time
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

  useEffect(() => {
    onOhlcChangeRef.current = onOhlcChange
    onTimeClickRef.current = onTimeClick
  }, [onOhlcChange, onTimeClick])

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

    return () => {
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

    seriesRef.current.setData(candles as CandlestickData<Time>[])
    volumeSeriesRef.current?.setData(
      volumes.map((point, index) => ({
        ...point,
        color:
          candles[index] && candles[index].close >= candles[index].open
            ? 'rgba(229, 72, 77, 0.45)'
            : 'rgba(59, 130, 246, 0.45)',
      })) as HistogramData<Time>[],
    )
    for (const [window, series] of Object.entries(maSeriesRef.current)) {
      series.setData((ma[window] ?? []) as LineData<Time>[])
    }
    chart.timeScale().fitContent()
  }, [candles, volumes, ma, visibleIndicators])

  useEffect(() => {
    markersApiRef.current?.setMarkers(markers.map(toSeriesMarker))
  }, [markers, candles])

  useEffect(() => {
    highlightRef.current?.setRange(highlightRange)
  }, [highlightRange, candles])

  return <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
}
