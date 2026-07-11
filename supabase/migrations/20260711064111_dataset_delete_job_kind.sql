-- 데이터셋 삭제 시도를 durable/재시도 가능한 job으로 남기기 위한 kind 추가 (docs/06 §7).
-- forward-only: 기존 마이그레이션은 수정하지 않는다.

alter table public.jobs drop constraint jobs_kind_check;
alter table public.jobs add constraint jobs_kind_check
  check (kind in ('preprocess_batch', 'diagnostics', 'training', 'dataset_delete'));
