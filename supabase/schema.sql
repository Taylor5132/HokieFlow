create extension if not exists "pgcrypto";

create table if not exists public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    email text not null unique,
    full_name text,
    avatar_url text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.saved_plans (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    title text not null default 'Saved plan',
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists saved_plans_user_id_idx on public.saved_plans(user_id);

alter table public.profiles enable row level security;
alter table public.saved_plans enable row level security;

create policy "profiles_are_viewable_by_self"
on public.profiles
for select
using (auth.uid() = id);

create policy "profiles_are_insertable_by_self"
on public.profiles
for insert
with check (auth.uid() = id);

create policy "profiles_are_updatable_by_self"
on public.profiles
for update
using (auth.uid() = id)
with check (auth.uid() = id);

create policy "profiles_are_deletable_by_self"
on public.profiles
for delete
using (auth.uid() = id);

create policy "saved_plans_are_viewable_by_self"
on public.saved_plans
for select
using (auth.uid() = user_id);

create policy "saved_plans_are_insertable_by_self"
on public.saved_plans
for insert
with check (auth.uid() = user_id);

create policy "saved_plans_are_updatable_by_self"
on public.saved_plans
for update
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

create policy "saved_plans_are_deletable_by_self"
on public.saved_plans
for delete
using (auth.uid() = user_id);

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, email, full_name, avatar_url)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data->>'full_name', new.raw_user_meta_data->>'name'),
    new.raw_user_meta_data->>'avatar_url'
  )
  on conflict (id) do update
    set email = excluded.email,
        full_name = coalesce(excluded.full_name, public.profiles.full_name),
        avatar_url = coalesce(excluded.avatar_url, public.profiles.avatar_url),
        updated_at = now();
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
after insert on auth.users
for each row execute function public.handle_new_user();
