-- Run in the Supabase SQL Editor for the SAME project as Azure's SUPABASE_URL.
-- Repairs PGRST205 for saved_plans without modifying profiles or deleting data.
-- Re-running is safe when the existing table matches supabase/schema.sql.
begin;

create table if not exists public.saved_plans (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    title text not null default 'Saved plan',
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists saved_plans_user_id_idx on public.saved_plans(user_id);
alter table public.saved_plans enable row level security;
grant select, insert, update, delete on public.saved_plans to authenticated;

do $$
begin
    if not exists (select 1 from pg_policies where schemaname = 'public'
                   and tablename = 'saved_plans' and policyname = 'saved_plans_are_viewable_by_self') then
        create policy saved_plans_are_viewable_by_self on public.saved_plans
            for select to authenticated using (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                   and tablename = 'saved_plans' and policyname = 'saved_plans_are_insertable_by_self') then
        create policy saved_plans_are_insertable_by_self on public.saved_plans
            for insert to authenticated with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                   and tablename = 'saved_plans' and policyname = 'saved_plans_are_updatable_by_self') then
        create policy saved_plans_are_updatable_by_self on public.saved_plans
            for update to authenticated using (auth.uid() = user_id)
            with check (auth.uid() = user_id);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'public'
                   and tablename = 'saved_plans' and policyname = 'saved_plans_are_deletable_by_self') then
        create policy saved_plans_are_deletable_by_self on public.saved_plans
            for delete to authenticated using (auth.uid() = user_id);
    end if;
end $$;

notify pgrst, 'reload schema';
commit;

-- Check schema only; does not read anyone's saved schedule.
select table_schema, table_name, column_name, data_type
from information_schema.columns
where table_schema = 'public' and table_name = 'saved_plans'
order by ordinal_position;
