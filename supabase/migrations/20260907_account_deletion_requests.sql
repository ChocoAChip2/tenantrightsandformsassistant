-- Account deletion with a 30-day grace period.
--
-- This is the first SQL checked into this repo: every table before it was
-- created by hand in the Supabase dashboard, which meant the schema existed
-- in exactly one place and nowhere reviewable. That's a bad habit to keep,
-- and it matters more than usual here, because this migration installs
-- something that DELETES USER ACCOUNTS on a schedule -- that belongs in code
-- review, not in a dashboard text box.
--
-- HOW THE FEATURE WORKS:
--   1. In Settings, a user double-confirms (types their own email address,
--      ticks an acknowledgement) that they want their account deleted.
--   2. routes.py inserts one row here with purge_after = now() + 30 days.
--      Nothing is deleted at that moment -- the account keeps working, so
--      an accidental or regretted request can simply be cancelled.
--   3. Cancelling deletes the row. That's the whole undo.
--   4. A daily pg_cron job calls purge_expired_account_deletions(), which
--      deletes the auth.users rows whose grace period has run out.
--
-- WHY THE PURGE RUNS IN THE DATABASE INSTEAD OF THE FLASK APP:
-- Deleting an auth.users row needs privileges the web app does not have --
-- it authenticates with the anon key plus the signed-in user's JWT, and
-- deliberately has no service-role key anywhere in its environment. Giving
-- the web process a service-role key just to run a nightly cleanup would
-- hand every future bug in that process the ability to read or delete any
-- user's data. Running it as a SECURITY DEFINER function on a pg_cron
-- schedule keeps that privilege inside the database, and has the side
-- benefit of still firing when the Render service is asleep or the GitHub
-- Actions scheduler has quietly disabled itself (see keepalive.yml for how
-- that has bitten this project before).

-- ---------------------------------------------------------------------
-- 1. The request table
-- ---------------------------------------------------------------------

create table if not exists public.account_deletion_requests (
  -- One pending request per user, so the primary key is the user id
  -- itself: re-requesting deletion updates the existing row instead of
  -- stacking up duplicates with different deadlines.
  user_id uuid primary key references auth.users (id) on delete cascade,
  requested_at timestamptz not null default now(),
  -- Stored as an absolute timestamp rather than computed at purge time, so
  -- the deadline a user was actually shown in Settings is the deadline that
  -- gets honoured, even if the grace period is changed later.
  purge_after timestamptz not null
);

comment on table public.account_deletion_requests is
  'Pending account deletions. A row here means the user asked to be deleted; purge_expired_account_deletions() removes the auth.users row once purge_after has passed. Deleting a row cancels the request.';

create index if not exists account_deletion_requests_purge_after_idx
  on public.account_deletion_requests (purge_after);

alter table public.account_deletion_requests enable row level security;

-- RLS: a user can see, create, update and cancel only their own request.
-- The purge function below is SECURITY DEFINER and bypasses these, which is
-- the point -- no client ever needs to read another user's row.
drop policy if exists "Users can view their own deletion request"
  on public.account_deletion_requests;
create policy "Users can view their own deletion request"
  on public.account_deletion_requests
  for select
  using (auth.uid() = user_id);

drop policy if exists "Users can request their own deletion"
  on public.account_deletion_requests;
create policy "Users can request their own deletion"
  on public.account_deletion_requests
  for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update their own deletion request"
  on public.account_deletion_requests;
create policy "Users can update their own deletion request"
  on public.account_deletion_requests
  for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "Users can cancel their own deletion request"
  on public.account_deletion_requests;
create policy "Users can cancel their own deletion request"
  on public.account_deletion_requests
  for delete
  using (auth.uid() = user_id);

-- ---------------------------------------------------------------------
-- 2. The purge function
-- ---------------------------------------------------------------------

-- Deletes auth.users rows whose grace period has expired, and returns how
-- many were removed. conversations.user_id, messages.user_id and this
-- table's own user_id are all ON DELETE CASCADE against auth.users, so this
-- single delete takes the account's conversations, messages and the request
-- row with it -- no manual ordering, and no way for one of those steps to
-- succeed while another silently fails.
--
-- The where clause is the only thing standing between this and deleting the
-- wrong account, so it is deliberately narrow and static: a user is only
-- ever touched if they have a row in account_deletion_requests AND that
-- row's purge_after is already in the past. No dynamic SQL, no parameters,
-- nothing a caller can widen.
create or replace function public.purge_expired_account_deletions()
returns integer
language plpgsql
security definer
set search_path = public, auth, pg_temp
as $$
declare
  purged integer;
begin
  delete from auth.users u
  where u.id in (
    select r.user_id
    from public.account_deletion_requests r
    where r.purge_after <= now()
  );

  get diagnostics purged = row_count;

  if purged > 0 then
    raise log 'purge_expired_account_deletions: deleted % account(s)', purged;
  end if;

  return purged;
end;
$$;

comment on function public.purge_expired_account_deletions() is
  'Deletes auth.users rows whose account_deletion_requests.purge_after has passed. Cascades remove their conversations, messages and request row. Scheduled daily via pg_cron.';

-- This function is called by pg_cron (as the postgres superuser), never by
-- the app, so no client role gets execute permission on it.
revoke all on function public.purge_expired_account_deletions() from public;
revoke all on function public.purge_expired_account_deletions() from anon, authenticated;

-- ---------------------------------------------------------------------
-- 3. The daily schedule
-- ---------------------------------------------------------------------

create extension if not exists pg_cron;

-- Unschedule first so re-running this migration doesn't create a second
-- copy of the same job.
select cron.unschedule('purge-expired-account-deletions')
where exists (
  select 1 from cron.job where jobname = 'purge-expired-account-deletions'
);

-- 03:15 UTC daily. The exact time doesn't matter much (a request is already
-- 30 days old by the time it qualifies, so being up to a day late is
-- immaterial), but off-peak keeps it out of the way of real traffic.
select cron.schedule(
  'purge-expired-account-deletions',
  '15 3 * * *',
  $$select public.purge_expired_account_deletions();$$
);
