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
    quota_chars   integer not null default 1000,  -- monthly character allowance
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- Backfill column for existing installs that already have the table.
alter table public.profiles add column if not exists role text not null default 'user';

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
    notes                  text,
    updated_at             timestamptz not null default now()
);

-- Seed defaults. Re-running updates only if values differ (admin can
-- override via /api/admin/limits and won't be reset on schema reruns).
insert into public.plan_limits (plan, daily_uses, lifetime_uses, max_chars_per_request, monthly_chars, notes)
values
    ('free',  null, 1,    100,  100,   'Single trial: 1 generation ever, max 100 chars'),
    ('pro',   10,   null, 5000, 50000, 'Daily 10 generations, 5000 chars/request, 50k chars/month'),
    ('admin', null, null, null, null,  'Unlimited — used by ADMIN_EMAILS')
on conflict (plan) do nothing;

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
