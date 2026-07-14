create index if not exists overseas_master_symbol_trgm_idx
  on public.overseas_master using gin (symbol extensions.gin_trgm_ops)
  where active;

create index if not exists overseas_master_korean_name_trgm_idx
  on public.overseas_master using gin (korean_name extensions.gin_trgm_ops)
  where active;

create index if not exists overseas_master_english_name_trgm_idx
  on public.overseas_master using gin (english_name extensions.gin_trgm_ops)
  where active;

create or replace function public.search_overseas_master(
  query text,
  match_limit integer default 10
)
returns table (
  symbol text,
  name text,
  market text,
  exchange text,
  score real
)
language sql
stable
security invoker
set search_path = public, extensions
as $$
  with input as (
    select trim(query) as q, least(greatest(match_limit, 1), 20) as lim
  )
  select
    m.symbol,
    coalesce(nullif(m.korean_name, ''), m.english_name) as name,
    m.market,
    case m.market when 'NASDAQ' then 'ND' when 'NYSE' then 'NY' else 'NA' end,
    greatest(
      similarity(m.symbol, input.q),
      similarity(m.korean_name, input.q),
      similarity(m.english_name, input.q),
      case when m.symbol = upper(input.q) then 1.0 else 0.0 end,
      case when m.symbol ilike input.q || '%' then 0.95 else 0.0 end,
      case when m.korean_name ilike input.q || '%' then 0.9 else 0.0 end,
      case when m.english_name ilike input.q || '%' then 0.9 else 0.0 end
    )::real as score
  from public.overseas_master m, input
  where
    m.active
    and input.q <> ''
    and (
      m.symbol % input.q
      or m.korean_name % input.q
      or m.english_name % input.q
      or m.symbol ilike input.q || '%'
      or m.korean_name ilike '%' || input.q || '%'
      or m.english_name ilike '%' || input.q || '%'
    )
  order by score desc, m.market, m.symbol
  limit (select lim from input);
$$;

revoke all on function public.search_overseas_master(text, integer)
  from public, anon, authenticated;
grant execute on function public.search_overseas_master(text, integer) to service_role;
