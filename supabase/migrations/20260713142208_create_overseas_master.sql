create table public.overseas_master (
  market text not null check (market in ('NASDAQ', 'NYSE', 'AMEX')),
  symbol text not null,
  realtime_symbol text not null default '',
  korean_name text not null default '',
  english_name text not null default '',
  security_type text not null default '',
  currency text not null default 'USD',
  exchange_id text not null default '',
  exchange_code text not null default '',
  exchange_name text not null default '',
  country_code text not null default 'US',
  base_price bigint,
  lot_size integer,
  active boolean not null default true,
  updated_at timestamptz not null,
  primary key (market, symbol)
);

comment on table public.overseas_master is
  'KIS NASDAQ/NYSE/AMEX symbol master; raw source rows are intentionally not stored';

create index overseas_master_symbol_idx
  on public.overseas_master (symbol)
  where active;

create index overseas_master_market_idx
  on public.overseas_master (market)
  where active;

alter table public.overseas_master enable row level security;
revoke all on table public.overseas_master from public, anon, authenticated;
grant all on table public.overseas_master to service_role;
