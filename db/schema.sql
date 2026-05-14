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
-- monthly_usage view: rolling 30-day per-user char total
-- ─────────────────────────────────────────────────────────────────────
create or replace view public.monthly_usage as
select
    user_id,
    coalesce(sum(chars), 0)::int as chars_30d,
    count(*)::int                as events_30d
from public.usage_events
where created_at > now() - interval '30 days'
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
