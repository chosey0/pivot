import { useEffect, useState } from 'react'
import type { DatasetRow } from '../../api/client'
import { trainingApi, type ModelType, type SamplerType } from '../../api/training'

const MODEL_OPTIONS: { value: ModelType; label: string }[] = [
  { value: 'cnn1d_legacy_v1', label: 'cnn1d_legacy_v1 — 구 프로젝트 재현 베이스라인' },
  { value: 'cnn1d_temporal_v1', label: 'cnn1d_temporal_v1 — 시간축 kernel 비교 모델' },
]

function classCountsText(counts: Record<string, number>) {
  return ['0', '1', '2']
    .filter((label) => counts[label] !== undefined)
    .map((label) => `${label}: ${counts[label].toLocaleString()}`)
    .join(' · ')
}

interface Props {
  datasets: DatasetRow[] // ready 상태만 전달된다
  onCreated: (runId: number) => void
  onClose: () => void
}

export function NewRunForm({ datasets, onCreated, onClose }: Props) {
  const [name, setName] = useState('')
  const [datasetId, setDatasetId] = useState<number | null>(datasets[0]?.id ?? null)
  const [model, setModel] = useState<ModelType>('cnn1d_legacy_v1')
  const [epochs, setEpochs] = useState(20)
  const [batchSize, setBatchSize] = useState(64)
  const [learningRate, setLearningRate] = useState(0.001)
  const [sampler, setSampler] = useState<SamplerType>('none')
  const [seed, setSeed] = useState(42)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setDatasetId((current) =>
      current !== null && datasets.some((row) => row.id === current)
        ? current
        : datasets[0]?.id ?? null,
    )
  }, [datasets])

  const dataset = datasets.find((row) => row.id === datasetId) ?? null
  const split = dataset?.preset_snapshot?.split
  const valid =
    name.trim() !== '' &&
    datasetId !== null &&
    Number.isInteger(epochs) &&
    epochs >= 1 &&
    Number.isInteger(batchSize) &&
    batchSize >= 1 &&
    learningRate > 0 &&
    Number.isInteger(seed)

  async function submit() {
    if (!valid || datasetId === null) return
    setSubmitting(true)
    setError(null)
    try {
      const { run_id } = await trainingApi.createRun(name.trim(), datasetId, {
        model,
        epochs,
        batch_size: batchSize,
        learning_rate: learningRate,
        sampler,
        seed,
        scaling: 'sample_standard_v1',
        padding: 'zero_masked_v1',
        best_metric: 'val_macro_f1',
      })
      onCreated(run_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="control-section run-form">
      <div className="section-title-row">
        <h2>새 학습 run</h2>
        <button className="ghost" onClick={onClose} type="button">
          닫기
        </button>
      </div>
      {datasets.length === 0 ? (
        <p className="empty">ready 상태 데이터셋이 없습니다. 데이터셋 탭에서 먼저 생성하세요.</p>
      ) : (
        <>
          <div className="run-form-grid">
            <label className="field">
              run 이름
              <input
                onChange={(event) => setName(event.target.value)}
                placeholder="예: day20-legacy-baseline"
                type="text"
                value={name}
              />
            </label>
            <label className="field">
              데이터셋 (ready)
              <select
                onChange={(event) => setDatasetId(Number(event.target.value))}
                value={datasetId ?? ''}
              >
                {datasets.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.name} · {row.sample_count.toLocaleString()}샘플
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              모델
              <select
                onChange={(event) => setModel(event.target.value as ModelType)}
                value={model}
              >
                {MODEL_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              sampler
              <select
                onChange={(event) => setSampler(event.target.value as SamplerType)}
                value={sampler}
              >
                <option value="none">none — 원본 분포</option>
                <option value="weighted">weighted — 클래스 가중 샘플링</option>
              </select>
            </label>
            <label className="field">
              epochs
              <input
                min={1}
                onChange={(event) => setEpochs(Number(event.target.value))}
                type="number"
                value={epochs}
              />
            </label>
            <label className="field">
              batch size
              <input
                min={1}
                onChange={(event) => setBatchSize(Number(event.target.value))}
                type="number"
                value={batchSize}
              />
            </label>
            <label className="field">
              learning rate
              <input
                min={0}
                onChange={(event) => setLearningRate(Number(event.target.value))}
                step={0.0001}
                type="number"
                value={learningRate}
              />
            </label>
            <label className="field">
              seed
              <input
                onChange={(event) => setSeed(Number(event.target.value))}
                type="number"
                value={seed}
              />
            </label>
          </div>

          <div className="run-contract" title="M4 베이스라인 계약으로 고정된 값 (docs/07 §2)">
            <span>
              scaling <strong>sample_standard_v1</strong>
            </span>
            <span>
              padding <strong>zero_masked_v1</strong>
            </span>
            <span>
              모델 선택 <strong>val_macro_f1</strong>
            </span>
          </div>

          {dataset && (
            <div className="run-dataset-snapshot">
              <strong>
                {dataset.name} · {dataset.timeframe} ·{' '}
                {dataset.preset_snapshot?.preset_name ?? `프리셋 #${dataset.preset_id}`}
                {dataset.preset_snapshot?.preset_version
                  ? ` v${dataset.preset_snapshot.preset_version}`
                  : ''}
              </strong>
              <span>
                {dataset.sample_count.toLocaleString()}샘플 · {dataset.symbol_count}종목 · 클래스{' '}
                {classCountsText(dataset.class_counts)}
              </span>
              <span>features {dataset.feature_columns.join(', ')}</span>
              {split && (
                <span>
                  split {split.method} · seed {split.seed} ·{' '}
                  {Object.entries(split.ratios)
                    .map(([key, ratio]) => `${key} ${Math.round(ratio * 100)}%`)
                    .join(' / ')}
                </span>
              )}
            </div>
          )}

          {error ? <p className="error">오류: {error}</p> : null}
          <button className="primary" disabled={!valid || submitting} onClick={submit} type="button">
            {submitting ? '시작 중...' : '학습 시작'}
          </button>
        </>
      )}
    </section>
  )
}
