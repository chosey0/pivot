-- Inactive deployment history must not permanently pin a deletable training run.

alter table public.live_deployments
  drop constraint live_deployments_run_id_fkey,
  add constraint live_deployments_run_id_fkey
    foreign key (run_id) references public.training_runs(id) on delete cascade,
  drop constraint live_deployments_artifact_id_fkey,
  add constraint live_deployments_artifact_id_fkey
    foreign key (artifact_id) references public.training_artifacts(id) on delete cascade;

comment on table public.live_deployments is
  'M5 activation history; inactive rows are removed with their deleted training run.';
