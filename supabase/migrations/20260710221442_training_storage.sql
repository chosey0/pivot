-- Training metadata lives in Postgres. Large dataset/checkpoint binaries live in
-- private Supabase Storage buckets and are referenced by immutable object paths.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  (
    'pivot-datasets',
    'pivot-datasets',
    false,
    52428800,
    array['application/vnd.apache.parquet', 'application/octet-stream']
  ),
  (
    'pivot-models',
    'pivot-models',
    false,
    52428800,
    array['application/octet-stream', 'application/json', 'text/plain']
  )
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types,
  updated_at = now();

create table public.training_presets (
  id bigint generated always as identity primary key,
  name text not null,
  version integer not null default 1 check (version > 0),
  schema_version integer not null default 1 check (schema_version > 0),
  preset jsonb not null check (jsonb_typeof(preset) = 'object'),
  archived_at timestamptz,
  created_at timestamptz not null default now(),
  unique (name, version)
);

create table public.jobs (
  id bigint generated always as identity primary key,
  kind text not null check (kind in ('preprocess_batch', 'diagnostics', 'training')),
  status text not null default 'queued'
    check (status in ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  payload jsonb not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
  result jsonb check (result is null or jsonb_typeof(result) = 'object'),
  error text,
  completed_items integer not null default 0 check (completed_items >= 0),
  total_items integer not null default 0 check (total_items >= 0),
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz
);

create table public.job_events (
  id bigint generated always as identity primary key,
  job_id bigint not null references public.jobs(id) on delete cascade,
  sequence integer not null check (sequence >= 0),
  event_type text not null,
  payload jsonb not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
  created_at timestamptz not null default now(),
  unique (job_id, sequence)
);

create table public.datasets (
  id bigint generated always as identity primary key,
  name text not null unique,
  preset_id bigint not null references public.training_presets(id) on delete restrict,
  preset_snapshot jsonb not null check (jsonb_typeof(preset_snapshot) = 'object'),
  timeframe text not null,
  status text not null default 'building'
    check (status in ('building', 'ready', 'failed')),
  feature_columns text[] not null check (cardinality(feature_columns) > 0),
  sample_count bigint not null default 0 check (sample_count >= 0),
  symbol_count integer not null default 0 check (symbol_count >= 0),
  class_counts jsonb not null default '{}'::jsonb
    check (jsonb_typeof(class_counts) = 'object'),
  failure_message text,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);

create table public.dataset_symbols (
  dataset_id bigint not null references public.datasets(id) on delete cascade,
  symbol text not null,
  split text check (split is null or split in ('train', 'validation', 'test')),
  status text not null default 'pending'
    check (status in ('pending', 'running', 'ready', 'failed')),
  sample_count bigint not null default 0 check (sample_count >= 0),
  class_counts jsonb not null default '{}'::jsonb
    check (jsonb_typeof(class_counts) = 'object'),
  length_stats jsonb not null default '{}'::jsonb
    check (jsonb_typeof(length_stats) = 'object'),
  error text,
  primary key (dataset_id, symbol)
);

create table public.dataset_shards (
  id bigint generated always as identity primary key,
  dataset_id bigint not null,
  symbol text not null,
  shard_index integer not null check (shard_index >= 0),
  bucket text not null default 'pivot-datasets'
    check (bucket = 'pivot-datasets'),
  object_path text not null,
  content_type text not null default 'application/vnd.apache.parquet',
  size_bytes bigint not null check (size_bytes >= 0),
  row_count bigint not null check (row_count >= 0),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  feature_schema jsonb not null check (jsonb_typeof(feature_schema) = 'object'),
  created_at timestamptz not null default now(),
  unique (dataset_id, symbol, shard_index),
  unique (bucket, object_path),
  foreign key (dataset_id, symbol)
    references public.dataset_symbols(dataset_id, symbol) on delete cascade
);

create table public.diagnostic_reports (
  id bigint generated always as identity primary key,
  target_type text not null check (target_type in ('raw_cache', 'preset', 'dataset')),
  preset_id bigint references public.training_presets(id) on delete set null,
  dataset_id bigint references public.datasets(id) on delete cascade,
  status text not null check (status in ('passed', 'warning', 'failed')),
  summary jsonb not null default '{}'::jsonb check (jsonb_typeof(summary) = 'object'),
  report jsonb not null check (jsonb_typeof(report) = 'object'),
  created_at timestamptz not null default now()
);

create table public.training_runs (
  id bigint generated always as identity primary key,
  name text not null,
  dataset_id bigint not null references public.datasets(id) on delete restrict,
  job_id bigint references public.jobs(id) on delete set null,
  status text not null default 'queued'
    check (status in ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  config jsonb not null check (jsonb_typeof(config) = 'object'),
  dataset_snapshot jsonb not null check (jsonb_typeof(dataset_snapshot) = 'object'),
  device text,
  best_epoch integer check (best_epoch is null or best_epoch >= 0),
  best_metric_name text,
  best_metric_value double precision,
  error text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz
);

create table public.training_epochs (
  run_id bigint not null references public.training_runs(id) on delete cascade,
  epoch integer not null check (epoch >= 0),
  metrics jsonb not null check (jsonb_typeof(metrics) = 'object'),
  created_at timestamptz not null default now(),
  primary key (run_id, epoch)
);

create table public.evaluations (
  id bigint generated always as identity primary key,
  run_id bigint not null references public.training_runs(id) on delete cascade,
  dataset_id bigint not null references public.datasets(id) on delete restrict,
  metrics jsonb not null check (jsonb_typeof(metrics) = 'object'),
  confusion_matrix jsonb not null check (jsonb_typeof(confusion_matrix) in ('array', 'object')),
  per_class_metrics jsonb not null check (jsonb_typeof(per_class_metrics) = 'object'),
  created_at timestamptz not null default now()
);

create table public.training_artifacts (
  id bigint generated always as identity primary key,
  run_id bigint not null references public.training_runs(id) on delete cascade,
  epoch integer check (epoch is null or epoch >= 0),
  kind text not null
    check (kind in ('checkpoint', 'best_checkpoint', 'scaler', 'history', 'log', 'report')),
  bucket text not null default 'pivot-models'
    check (bucket = 'pivot-models'),
  object_path text not null,
  content_type text not null default 'application/octet-stream',
  size_bytes bigint not null check (size_bytes >= 0),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  metadata jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
  created_at timestamptz not null default now(),
  unique (bucket, object_path)
);

create index training_presets_active_name_idx
  on public.training_presets (name, version desc)
  where archived_at is null;
create index jobs_status_created_idx on public.jobs (status, created_at desc);
create index datasets_status_created_idx on public.datasets (status, created_at desc);
create index datasets_preset_id_idx on public.datasets (preset_id);
create index diagnostic_reports_preset_id_idx on public.diagnostic_reports (preset_id);
create index diagnostic_reports_dataset_id_idx on public.diagnostic_reports (dataset_id);
create index diagnostic_reports_target_created_idx
  on public.diagnostic_reports (target_type, created_at desc);
create index training_runs_dataset_id_idx on public.training_runs (dataset_id);
create index training_runs_job_id_idx on public.training_runs (job_id);
create index training_runs_status_created_idx
  on public.training_runs (status, created_at desc);
create index evaluations_run_id_idx on public.evaluations (run_id);
create index evaluations_dataset_id_idx on public.evaluations (dataset_id);
create index training_artifacts_run_kind_idx
  on public.training_artifacts (run_id, kind, created_at desc);

alter table public.training_presets enable row level security;
alter table public.jobs enable row level security;
alter table public.job_events enable row level security;
alter table public.datasets enable row level security;
alter table public.dataset_symbols enable row level security;
alter table public.dataset_shards enable row level security;
alter table public.diagnostic_reports enable row level security;
alter table public.training_runs enable row level security;
alter table public.training_epochs enable row level security;
alter table public.evaluations enable row level security;
alter table public.training_artifacts enable row level security;

revoke all on table
  public.training_presets,
  public.jobs,
  public.job_events,
  public.datasets,
  public.dataset_symbols,
  public.dataset_shards,
  public.diagnostic_reports,
  public.training_runs,
  public.training_epochs,
  public.evaluations,
  public.training_artifacts
from anon, authenticated;

grant select, insert, update, delete on table
  public.training_presets,
  public.jobs,
  public.job_events,
  public.datasets,
  public.dataset_symbols,
  public.dataset_shards,
  public.diagnostic_reports,
  public.training_runs,
  public.training_epochs,
  public.evaluations,
  public.training_artifacts
to service_role;

grant usage, select on sequence
  public.training_presets_id_seq,
  public.jobs_id_seq,
  public.job_events_id_seq,
  public.datasets_id_seq,
  public.dataset_shards_id_seq,
  public.diagnostic_reports_id_seq,
  public.training_runs_id_seq,
  public.evaluations_id_seq,
  public.training_artifacts_id_seq
to service_role;

comment on table public.dataset_shards is
  'Metadata for immutable parquet objects in the private pivot-datasets Storage bucket.';
comment on table public.training_artifacts is
  'Metadata for immutable model artifacts in the private pivot-models Storage bucket.';
