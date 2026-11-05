-- ClipStage Supabase profile setup (idempotent - safe to re-run).
-- Run in the Supabase SQL editor.  Also disable public sign-ups under Authentication.

create table if not exists public.clipstage_profiles (
  user_id       uuid primary key references auth.users(id) on delete cascade,
  role          text not null check (role in ('admin', 'editor')),
  valid_through date not null,
  -- NEW (optional): bind a personal account to ONE editor name from editors.json.
  -- NULL = shared/legacy account that may act as any listed editor (old behaviour).
  editor_name   text,
  created_at    timestamptz not null default now()
);

alter table public.clipstage_profiles add column if not exists editor_name text;

alter table public.clipstage_profiles enable row level security;

-- Remove EVERY existing policy on this table, whatever it was called.
-- Older setups (or hand-made policies in the dashboard) had an admin policy that selected
-- from this same table inside its own policy; Postgres rejects that with
-- "infinite recursion detected in policy" (error 42P17) and ClipStage login then fails
-- with 503 "Could not read Supabase account profile". Dropping by name missed policies
-- with other names, so drop them all and recreate the one policy the app needs.
-- The app only ever READS a user's own profile; manage rows here or in the Table Editor
-- (both bypass RLS).
do $$
declare pol record;
begin
  for pol in
    select policyname from pg_policies
    where schemaname = 'public' and tablename = 'clipstage_profiles'
  loop
    execute format('drop policy %I on public.clipstage_profiles', pol.policyname);
  end loop;
end $$;

create policy "clipstage_profiles_self_read"
  on public.clipstage_profiles
  for select to authenticated
  using ((select auth.uid()) = user_id);

revoke all on public.clipstage_profiles from anon, authenticated;
grant select on public.clipstage_profiles to authenticated;

-- Add / update accounts by e-mail (no UUID copy-paste needed):
--
-- insert into public.clipstage_profiles (user_id, role, valid_through, editor_name)
-- select id, 'admin', '2026-12-31'::date, null
-- from auth.users where email = 'admin@example.com'
-- on conflict (user_id) do update
--   set role = excluded.role, valid_through = excluded.valid_through, editor_name = excluded.editor_name;
--
-- insert into public.clipstage_profiles (user_id, role, valid_through, editor_name)
-- select id, 'editor', '2026-12-31'::date, 'GOKUL'
-- from auth.users where email = 'gokul@example.com'
-- on conflict (user_id) do update
--   set role = excluded.role, valid_through = excluded.valid_through, editor_name = excluded.editor_name;

select user_id, role, valid_through, editor_name from public.clipstage_profiles order by role, editor_name;
