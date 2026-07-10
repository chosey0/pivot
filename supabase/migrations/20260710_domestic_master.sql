create extension if not exists pg_trgm;

create table if not exists public.domestic_master (
  symbol text primary key,
  name text not null,
  market text not null check (market in ('KOSPI', 'KOSDAQ')),
  standard_code text not null default '',
  security_type text not null default '',
  listed_date text not null default '',
  active boolean not null default true,
  raw jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  search_text text generated always as (
    symbol || ' ' || name || ' ' || coalesce(standard_code, '')
  ) stored
);

alter table public.domestic_master enable row level security;

create index if not exists domestic_master_name_trgm_idx
  on public.domestic_master using gin (name gin_trgm_ops);

create index if not exists domestic_master_search_text_trgm_idx
  on public.domestic_master using gin (search_text gin_trgm_ops);

create index if not exists domestic_master_market_idx
  on public.domestic_master (market)
  where active;

create or replace function public.search_domestic_master(
  query text,
  match_limit integer default 10
)
returns table (
  symbol text,
  name text,
  market text,
  score real
)
language sql
stable
as $$
  with input as (
    select trim(query) as q, least(greatest(match_limit, 1), 20) as lim
  )
  select
    m.symbol,
    m.name,
    m.market,
    greatest(
      similarity(m.search_text, input.q),
      case when m.symbol = input.q then 1.0 else 0.0 end,
      case when m.symbol ilike input.q || '%' then 0.95 else 0.0 end,
      case when m.name ilike input.q || '%' then 0.9 else 0.0 end
    )::real as score
  from public.domestic_master m, input
  where
    m.active
    and input.q <> ''
    and (
      m.search_text % input.q
      or m.symbol ilike input.q || '%'
      or m.name ilike '%' || input.q || '%'
    )
  order by score desc, m.market, m.symbol
  limit (select lim from input);
$$;
