import { useState } from 'react'
import type { PredictionEventData } from '../../api/live'
import { formatEventTime } from './time'

const LABEL_TEXT: Record<number, string> = { 0: '저점', 1: '고점', 2: '무시' }

function predictionKey(row: PredictionEventData) {
  return `${row.symbol}:${row.timeframe}:${row.time}:${row.deployment_id}`
}

/**
 * 최근 판정 로그 — cls3 모델은 비프랙탈 음성 샘플을 학습하지 않았으므로
 * 출력은 실험적 후보 점수로만 표기한다 (docs/08 §5). 매매 신호로 표현하지 않는다.
 */
export function PredictionLog({ predictions }: { predictions: PredictionEventData[] }) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null)

  const rows = [...predictions].reverse()
  const selected = rows.find((row) => predictionKey(row) === selectedKey) ?? null

  if (rows.length === 0) {
    return <p className="empty">아직 판정이 없습니다. 봉이 마감되면 후보 점수가 기록됩니다.</p>
  }

  return (
    <div className="live-pred-layout">
      <table className="live-pred-table">
        <thead>
          <tr>
            <th>시각</th>
            <th>종목</th>
            <th>판정</th>
            <th>저점</th>
            <th>고점</th>
            <th>무시</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = predictionKey(row)
            return (
              <tr
                className={key === selectedKey ? 'selected' : undefined}
                key={key}
                onClick={() => setSelectedKey(key === selectedKey ? null : key)}
              >
                <td>{formatEventTime(row.time)}</td>
                <td>{row.symbol}</td>
                <td>
                  <span className={`live-pred-class c${row.selected_class}`}>
                    {row.selected_class} {LABEL_TEXT[row.selected_class]}
                  </span>
                </td>
                {row.scores.map((score, label) => (
                  <td
                    className={label === row.selected_class ? 'live-score-selected' : undefined}
                    key={label}
                  >
                    {(score * 100).toFixed(1)}%
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>

      <div className="live-pred-detail">
        {selected ? (
          <>
            <div className="live-pred-detail-head">
              <strong>
                {selected.symbol} · {formatEventTime(selected.time)}
              </strong>
              <span className={`live-pred-class c${selected.selected_class}`}>
                {selected.selected_class} {LABEL_TEXT[selected.selected_class]}
              </span>
            </div>
            <div className="live-score-bars">
              {selected.scores.map((score, label) => (
                <div className="live-score-row" key={label}>
                  <span>
                    {label} {LABEL_TEXT[label]}
                  </span>
                  <div className="live-score-track">
                    <div
                      className={
                        label === selected.selected_class
                          ? 'live-score-fill selected'
                          : 'live-score-fill'
                      }
                      style={{ width: `${Math.round(score * 100)}%` }}
                    />
                  </div>
                  <em>{(score * 100).toFixed(1)}%</em>
                </div>
              ))}
            </div>
            <h4>후보 윈도우</h4>
            {selected.candidate_windows.map((window, index) => (
              <dl className="live-window-meta" key={index}>
                <div>
                  <dt>pairing</dt>
                  <dd>
                    {window.pairing_rule}
                    {window.shared_window ? ' · shared' : ''}
                  </dd>
                </div>
                <div>
                  <dt>anchor</dt>
                  <dd>
                    {window.anchor_kind === 'low' ? '저점' : '고점'} ·{' '}
                    {formatEventTime(window.anchor_time)} (#{window.anchor_position})
                  </dd>
                </div>
                <div>
                  <dt>구간</dt>
                  <dd>
                    {formatEventTime(window.start)} ~ {formatEventTime(window.end)}
                  </dd>
                </div>
              </dl>
            ))}
            <p className="live-disclaimer">
              프랙탈 후보 시퀀스 조건의 실험적 후보 점수입니다. 매매 신호가 아닙니다.
            </p>
          </>
        ) : (
          <p className="empty">로그 행을 클릭하면 점수와 후보 윈도우 상세를 보여줍니다.</p>
        )}
      </div>
    </div>
  )
}
