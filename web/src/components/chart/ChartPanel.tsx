import type { ReactNode } from 'react'

interface ChartPanelProps {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
  error?: string | null
  message?: string | null
  loading?: boolean
  loadingText?: string
  emptyText?: string
  hasContent: boolean
  overlay?: ReactNode
  legend?: ReactNode
  children: ReactNode
  footer?: ReactNode
}

export function ChartPanel({
  title,
  subtitle,
  actions,
  error = null,
  message = null,
  loading = false,
  loadingText = '불러오는 중...',
  emptyText = '표시할 차트가 없습니다.',
  hasContent,
  overlay,
  legend,
  children,
  footer,
}: ChartPanelProps) {
  return (
    <section className="chart-panel">
      <div className="chart-toolbar">
        <div>
          <h2>{title}</h2>
          {subtitle ? <span>{subtitle}</span> : null}
        </div>
        {actions}
      </div>
      {error ? <p className="error">오류: {error}</p> : null}
      {message && !error ? <p className="message">{message}</p> : null}
      <div className="chart-area">
        {loading && !hasContent ? <p className="empty">{loadingText}</p> : null}
        {overlay}
        {hasContent ? (
          <>
            {legend}
            {children}
          </>
        ) : (
          !loading && <p className="empty">{emptyText}</p>
        )}
      </div>
      {footer}
    </section>
  )
}
