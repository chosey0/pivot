import { fetchJson } from './client'

// docs/07_m4_implementation_plan.md §5 계약을 그대로 옮긴 타입. 임의로 확장하지 않는다.

export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type ModelType = 'cnn1d_legacy_v1' | 'cnn1d_temporal_v1'
export type SamplerType = 'none' | 'weighted'

export interface TrainingConfig {
  model: ModelType
  epochs: number
  batch_size: number
  learning_rate: number
  sampler: SamplerType
  seed: number
  scaling: 'sample_standard_v1'
  padding: 'zero_masked_v1'
  best_metric: 'val_macro_f1'
}

export interface RunSummary {
  id: number
  name: string
  dataset_id: number
  dataset_name: string
  job_id: number | null
  status: RunStatus
  config: TrainingConfig
  device: string | null
  best_epoch: number | null
  best_metric_name: string | null
  best_metric_value: number | null
  error: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface EpochMetrics {
  train_loss: number
  train_accuracy: number
  train_macro_f1: number
  validation_loss: number
  validation_accuracy: number
  validation_macro_f1: number
  learning_rate: number
}

export interface EpochRow {
  run_id: number
  epoch: number
  metrics: EpochMetrics
  created_at: string
}

export interface ClassMetrics {
  precision: number
  recall: number
  f1: number
  support: number
}

export type EvaluationSplit = 'validation' | 'test'

export interface EvaluationResult {
  id: number
  run_id: number
  dataset_id: number
  split: EvaluationSplit
  metrics: Record<string, number>
  confusion_matrix: number[][]
  per_class_metrics: Record<'0' | '1' | '2', ClassMetrics>
  created_at: string
}

export type ArtifactKind =
  | 'checkpoint'
  | 'best_checkpoint'
  | 'scaler'
  | 'history'
  | 'log'
  | 'report'

export interface ArtifactSummary {
  id: number
  epoch: number | null
  kind: ArtifactKind
  size_bytes: number
  sha256: string
  metadata: Record<string, unknown>
  created_at: string
}

export interface RunDetail {
  run: RunSummary
  epochs: EpochRow[]
  evaluations: EvaluationResult[]
  artifacts: ArtifactSummary[]
}

export interface PredictionPoint {
  sample_index: number
  time: string | number
  actual_label: 0 | 1 | 2
  predicted_label: 0 | 1 | 2
  probabilities: [number, number, number]
  correct: boolean
}

export interface PredictionEvaluation {
  run_id: number
  dataset_id: number
  symbol: string
  timeframe: string
  split: EvaluationSplit
  points: PredictionPoint[]
}

export const trainingApi = {
  runs: () => fetchJson<RunSummary[]>('/api/runs'),
  createRun: (name: string, datasetId: number, config: TrainingConfig) =>
    fetchJson<{ run_id: number; job_id: number }>('/api/runs', {
      method: 'POST',
      body: JSON.stringify({ name, dataset_id: datasetId, config }),
    }),
  runDetail: (runId: number) => fetchJson<RunDetail>(`/api/runs/${runId}`),
  stopRun: (runId: number) =>
    fetchJson<{ run_id: number; status: 'cancelled' }>(`/api/runs/${runId}/stop`, {
      method: 'POST',
    }),
  evaluate: (runId: number, symbol: string, split: EvaluationSplit) =>
    fetchJson<PredictionEvaluation>(`/api/runs/${runId}/evaluate`, {
      method: 'POST',
      body: JSON.stringify({ symbol, split }),
    }),
  eventsUrl: (runId: number) => `/api/runs/${runId}/events`,
}
