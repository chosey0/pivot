import { useEffect, useRef } from 'react'
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
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

interface Props {
  candles: Candle[]
  volumes?: VolumePoint[]
  ma?: Record<string, LinePoint[]>
  visibleIndicators?: VisibleIndicators
  onOhlcChange?: (point: OhlcPoint) => void
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
  onOhlcChange,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const maSeriesRef = useRef<Record<string, ISeriesApi<'Line'>>>({})
  const onOhlcChangeRef = useRef(onOhlcChange)

  useEffect(() => {
    onOhlcChangeRef.current = onOhlcChange
  }, [onOhlcChange])

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

    return () => {
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      volumeSeriesRef.current = null
      maSeriesRef.current = {}
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

  return <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
}
