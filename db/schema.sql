-- SastaSpeech database schema
-- Run this in Supabase SQL Editor after creating the project.
-- Re-runnable: every CREATE uses IF NOT EXISTS where possible.

-- ─────────────────────────────────────────────────────────────────────
-- profiles: 1:1 with auth.users, holds plan + display info
-- ─────────────────────────────────────────────────────────────────────
create table if not exists public.profiles (
    user_id       uuid primary key references auth.users(id) on delete cascade,
    email         text,
    display_name  text,
    role          text not null default 'user',   -- user | admin | pro
    plan          text not null default 'free',   -- free | starter | pro
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- Backfill column for existing installs that already have the table.
alter table public.profiles add column if not exists role text not null default 'user';

-- Drop the legacy per-user quota column. Enforcement now lives in
-- plan_limits.monthly_chars (and friends) keyed by plan/role, so
-- profiles.quota_chars was only ever written, never read.
alter table public.profiles drop column if exists quota_chars;

-- Auto-create a profile row whenever a new auth user signs up.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
as $$
begin
    insert into public.profiles (user_id, email)
    values (new.id, new.email)
    on conflict (user_id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();


-- ─────────────────────────────────────────────────────────────────────
-- plan_limits: per-plan rules — what each tier can do
-- All limits NULLABLE: null means "unlimited / no enforcement".
-- Admin can read + update these via /api/admin/limits.
-- ─────────────────────────────────────────────────────────────────────
create table if not exists public.plan_limits (
    plan                   text primary key,        -- 'free' | 'pro' | 'admin' | ...
    daily_uses             integer,                 -- max TTS calls per 24h
    lifetime_uses          integer,                 -- max TTS calls ever
    max_chars_per_request  integer,                 -- max script size per call
    monthly_chars          integer,                 -- total chars per 30 days
    -- Provider whitelists. Lookup order at request time:
    --   role='admin'   → plan_limits[plan='admin']
    --   everyone else  → plan_limits[plan=user.plan]
    -- Empty / null array means "no providers allowed" — request will 403.
    llm_providers          text[] not null default array['gemini'],      -- text models (LLM)
    tts_providers          text[] not null default array['elevenlabs'],  -- voice models (TTS)
    notes                  text,
    updated_at             timestamptz not null default now()
);

-- Idempotent column adds for existing deployments (no-op on fresh installs).
alter table public.plan_limits
    add column if not exists llm_providers text[] not null default array['gemini'];
alter table public.plan_limits
    add column if not exists tts_providers text[] not null default array['elevenlabs'];

-- Seed defaults. Re-running updates only if values differ (admin can
-- override via /api/admin/limits and won't be reset on schema reruns).
insert into public.plan_limits
    (plan, daily_uses, lifetime_uses, max_chars_per_request, monthly_chars,
     llm_providers, tts_providers, notes)
values
    ('free',  null, 1,    100,  100,
     array['gemini'], array['elevenlabs'],
     'Single trial: 1 generation ever, max 100 chars'),
    ('pro',   10,   null, 5000, 50000,
     array['gemini'], array['elevenlabs'],
     'Daily 10 generations, 5000 chars/request, 50k chars/month'),
    ('admin', null, null, null, null,
     array['gemini','ollama'], array['elevenlabs','parler','bark'],
     'Unlimited — admins (ADMIN_EMAILS) can pick any provider')
on conflict (plan) do nothing;

-- Backfill for deployments that had plan_limits rows BEFORE the
-- provider columns existed: the ALTER added them with the column
-- default ['gemini']/['elevenlabs'], and the INSERT above was a
-- no-op (on conflict do nothing). Promote those default values to
-- the intended seed for each plan — but ONLY when the row still
-- equals the column default, so we never clobber an admin's custom
-- edits made via /api/admin/limits.
update public.plan_limits
   set llm_providers = array['gemini','ollama']
 where plan = 'admin' and llm_providers = array['gemini'];

update public.plan_limits
   set tts_providers = array['elevenlabs','parler','bark']
 where plan = 'admin' and tts_providers = array['elevenlabs'];

alter table public.plan_limits enable row level security;

-- Anyone authenticated can READ their plan's limits (so frontend can
-- show "X/100 chars used"). Writes go through service role only.
drop policy if exists "plan_limits_read_all" on public.plan_limits;
create policy "plan_limits_read_all"
    on public.plan_limits for select
    using (auth.role() = 'authenticated');


-- ─────────────────────────────────────────────────────────────────────
-- usage_events: every billable action (TTS generation, etc.) logged
-- ─────────────────────────────────────────────────────────────────────
create table if not exists public.usage_events (
    id           bigserial primary key,
    user_id      uuid not null references auth.users(id) on delete cascade,
    kind         text not null,                   -- 'tts.generate', 'tts.regenerate', ...
    provider     text,                            -- 'elevenlabs' | 'parler' | 'bark' | ...
    chars        integer not null default 0,
    cost_usd     numeric(10,4) not null default 0,
    meta         jsonb,
    created_at   timestamptz not null default now()
);

create index if not exists usage_events_user_created_idx
    on public.usage_events (user_id, created_at desc);


-- ─────────────────────────────────────────────────────────────────────
-- usage_summary view: per-user rolling counts (24h / 30d / lifetime)
-- ─────────────────────────────────────────────────────────────────────
create or replace view public.usage_summary as
select
    user_id,
    coalesce(sum(case when created_at > now() - interval '1 day'  then chars else 0 end), 0)::int as chars_24h,
    coalesce(sum(case when created_at > now() - interval '30 days' then chars else 0 end), 0)::int as chars_30d,
    coalesce(sum(chars), 0)::int                                                                   as chars_total,
    coalesce(sum(case when created_at > now() - interval '1 day'  then 1     else 0 end), 0)::int as uses_24h,
    coalesce(sum(case when created_at > now() - interval '30 days' then 1     else 0 end), 0)::int as uses_30d,
    count(*)::int                                                                                  as uses_total
from public.usage_events
group by user_id;


-- ─────────────────────────────────────────────────────────────────────
-- Row Level Security — users can only see their own rows
-- ─────────────────────────────────────────────────────────────────────
alter table public.profiles      enable row level security;
alter table public.usage_events  enable row level security;

drop policy if exists "profiles_self_read"   on public.profiles;
drop policy if exists "profiles_self_update" on public.profiles;
drop policy if exists "usage_self_read"      on public.usage_events;

create policy "profiles_self_read"
    on public.profiles for select
    using (auth.uid() = user_id);

create policy "profiles_self_update"
    on public.profiles for update
    using (auth.uid() = user_id);

create policy "usage_self_read"
    on public.usage_events for select
    using (auth.uid() = user_id);

-- Inserts to usage_events go through service_role (backend), which
-- bypasses RLS. No insert policy for end-users.
