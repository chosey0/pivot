import type { EvaluationResult } from '../../api/training'

const LABEL_TEXT: Record<string, string> = { '0': '0 저점', '1': '1 고점', '2': '2 무시' }

function formatMetric(value: number) {
  return value.toLocaleString('ko-KR', { maximumFractionDigits: 4 })
}

function ConfusionMatrix({ matrix }: { matrix: number[][] }) {
  return (
    <table className="cm-table">
      <thead>
        <tr>
          <th scope="col">실제 \ 예측</th>
          {matrix.map((_, column) => (
            <th key={column} scope="col">
              {LABEL_TEXT[String(column)] ?? column}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {matrix.map((row, actual) => {
          const rowTotal = row.reduce((sum, count) => sum + count, 0)
          return (
            <tr key={actual}>
              <th scope="row">{LABEL_TEXT[String(actual)] ?? actual}</th>
              {row.map((count, predicted) => {
                const ratio = rowTotal > 0 ? count / rowTotal : 0
                return (
                  <td
                    className={actual === predicted ? 'cm-diagonal' : undefined}
                    key={predicted}
                    style={{ backgroundColor: `rgba(37, 99, 235, ${(ratio * 0.35).toFixed(3)})` }}
                    title={`실제 ${actual} → 예측 ${predicted}: ${count.toLocaleString()} (${(ratio * 100).toFixed(1)}%)`}
                  >
                    {count.toLocaleString()}
                  </td>
                )
              })}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

export function EvaluationReport({ evaluation }: { evaluation: EvaluationResult }) {
  const classKeys = ['0', '1', '2'] as const
  return (
    <div className="eval-report">
      <div className="eval-summary">
        {Object.entries(evaluation.metrics).map(([name, value]) => (
          <span className="eval-metric" key={name}>
            <em>{name}</em>
            <strong>{formatMetric(value)}</strong>
          </span>
        ))}
      </div>
      <div className="eval-tables">
        <div>
          <h4>Confusion matrix</h4>
          <ConfusionMatrix matrix={evaluation.confusion_matrix} />
        </div>
        <div>
          <h4>클래스별 지표</h4>
          <table className="per-class-table">
            <thead>
              <tr>
                <th>클래스</th>
                <th>precision</th>
                <th>recall</th>
                <th>F1</th>
                <th>support</th>
              </tr>
            </thead>
            <tbody>
              {classKeys.map((key) => {
                const metrics = evaluation.per_class_metrics[key]
                if (!metrics) return null
                return (
                  <tr key={key}>
                    <th scope="row">{LABEL_TEXT[key]}</th>
                    <td>{formatMetric(metrics.precision)}</td>
                    <td>{formatMetric(metrics.recall)}</td>
                    <td>{formatMetric(metrics.f1)}</td>
                    <td>{metrics.support.toLocaleString()}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
