-- Keep extensions outside the API-exposed public schema and pin function lookup.

create schema if not exists extensions;

alter extension pg_trgm set schema extensions;

alter function public.search_domestic_master(text, integer)
  set search_path = public, extensions;
