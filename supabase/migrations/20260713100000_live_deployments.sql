-- M5 active model pointer. Activation is transactional and keeps deployment history.

create table public.live_deployments (
  id bigint generated always as identity primary key,
  run_id bigint not null references public.training_runs(id) on delete restrict,
  artifact_id bigint not null references public.training_artifacts(id) on delete restrict,
  active boolean not null default true,
  activated_at timestamptz not null default now(),
  deactivated_at timestamptz,
  check (
    (active and deactivated_at is null)
    or (not active and deactivated_at is not null)
  )
);

create unique index live_deployments_one_active_idx
  on public.live_deployments (active)
  where active;

create index live_deployments_activated_idx
  on public.live_deployments (activated_at desc);

alter table public.live_deployments enable row level security;
revoke all on table public.live_deployments from public, anon, authenticated;
grant select, insert, update on table public.live_deployments to service_role;
grant usage, select on sequence public.live_deployments_id_seq to service_role;

create or replace function public.activate_live_deployment(
  target_run_id bigint,
  target_artifact_id bigint
)
returns setof public.live_deployments
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if not exists (
    select 1
    from public.training_runs
    where id = target_run_id and status = 'succeeded'
  ) then
    raise exception 'training run % is not deployable', target_run_id;
  end if;

  if not exists (
    select 1
    from public.training_artifacts
    where id = target_artifact_id
      and run_id = target_run_id
      and kind = 'best_checkpoint'
  ) then
    raise exception 'artifact % is not a best checkpoint for run %',
      target_artifact_id, target_run_id;
  end if;

  update public.live_deployments
  set active = false, deactivated_at = now()
  where active;

  return query
  insert into public.live_deployments (run_id, artifact_id)
  values (target_run_id, target_artifact_id)
  returning *;
end;
$$;

revoke all on function public.activate_live_deployment(bigint, bigint) from public;
revoke all on function public.activate_live_deployment(bigint, bigint)
  from anon, authenticated;
grant execute on function public.activate_live_deployment(bigint, bigint)
  to service_role;

comment on table public.live_deployments is
  'M5 immutable activation history with at most one active model deployment.';
