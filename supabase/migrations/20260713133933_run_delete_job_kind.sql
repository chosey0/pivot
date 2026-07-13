-- Run deletion is durable for the same object-first retry semantics as dataset deletion.
alter table public.jobs drop constraint jobs_kind_check;
alter table public.jobs add constraint jobs_kind_check
  check (kind in (
    'preprocess_batch',
    'diagnostics',
    'training',
    'dataset_delete',
    'run_delete'
  ));
