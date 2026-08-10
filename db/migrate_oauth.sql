-- ─────────────────────────────────────────────────────────────────────
-- Migration: Supabase auth  →  self-hosted Google OAuth.
--
-- user_id stops being an auth.users(id) UUID and becomes the Google account
-- id (text). Supabase is then storage/DB only. Run this ONCE, in the Supabase
-- SQL editor, WHEN you switch AUTH_PROVIDER=oauth.
--
-- ⚠️  This CLEARS all existing users + their data (per plan: current users are
--     disposable). After running, everyone signs in fresh via Google.
--
-- Order matters: RLS policies reference user_id, so they must be dropped
-- BEFORE the column type can be altered.
-- ─────────────────────────────────────────────────────────────────────

-- 1. Drop the Supabase auto-profile trigger (there's no auth.users insert now).
drop trigger if exists on_auth_user_created on auth.users;
drop function if exists public.handle_new_user();

-- 2. Drop every RLS policy that references user_id. They used auth.uid()
--    (a Supabase session function) which never matches our own auth, so all
--    access is via the service-role backend anyway. This MUST come before the
--    column-type change below (a column used in a policy can't be altered).
drop policy if exists "profiles_self_read"         on public.profiles;
drop policy if exists "profiles_self_update"       on public.profiles;
drop policy if exists "usage_self_read"            on public.usage_events;
drop policy if exists "payment_orders_self_read"   on public.payment_orders;
drop policy if exists "upgrade_requests_self_read" on public.upgrade_requests;
drop policy if exists "support_self_read"          on public.support_tickets;

-- 3. Wipe disposable user data (cascades to anything referencing it).
truncate table public.usage_events,
               public.payment_orders,
               public.upgrade_requests,
               public.support_tickets,
               public.profiles
        restart identity cascade;

-- 4. Drop the FKs to auth.users, then widen user_id to text (Google sub).
alter table public.profiles         drop constraint if exists profiles_user_id_fkey;
alter table public.usage_events      drop constraint if exists usage_events_user_id_fkey;
alter table public.payment_orders    drop constraint if exists payment_orders_user_id_fkey;
alter table public.upgrade_requests  drop constraint if exists upgrade_requests_user_id_fkey;
alter table public.support_tickets   drop constraint if exists support_tickets_user_id_fkey;

alter table public.profiles         alter column user_id type text using user_id::text;
alter table public.usage_events      alter column user_id type text using user_id::text;
alter table public.payment_orders    alter column user_id type text using user_id::text;
alter table public.upgrade_requests  alter column user_id type text using user_id::text;
alter table public.support_tickets   alter column user_id type text using user_id::text;

-- RLS stays ENABLED on every table (service-role bypasses it); with no
-- policies + no Supabase session, end-user clients get zero rows — which is
-- correct, since the frontend now reads everything through the backend.
