# Account schedule storage

The integrated Python server uses the existing `public.saved_plans` table in
`supabase/schema.sql`. No `account_data` table or new migration is required.
Keep the existing row-level-security policies enabled.

If production reports `PGRST205`, the table is missing from that project's
Data API schema/cache. In the Supabase project configured in Azure's
`SUPABASE_URL`, run `supabase/ensure_saved_plans.sql` in SQL Editor. It creates
the table when missing, installs the existing owner-only policies when absent,
grants authenticated access subject to RLS, and reloads the API schema cache.
It does not modify profile tables or delete existing rows. Pushing this SQL to
GitHub does **not** execute it in Supabase. After running it, refresh HokieFlow,
add an event, and refresh again to verify persistence.

`app/account_store.py` reserves one UUIDv5 row per user (namespace URL,
`hokieflow:account:<user-id>`). Its title is `HokieFlow account` and its JSONB
payload is `{kind: "hokieflow_account_v1", version: <integer>, data: {...}}`.
The data object contains `savedClass`, `reduceMotion`, `plans`, and `events`.
Existing, ordinary saved-plan rows are not read, updated, or deleted by this
adapter. A backend listing individual saved plans should exclude the reserved
row by its payload kind.

The browser still calls `PUT /api/account/data` and `GET /api/auth/me`. The
adapter passes the signed session's access token (or validated Bearer token)
to a fresh Supabase Data API client for every request. Password login and
registration load state using the newly issued session token. Database requests
therefore run under that user's RLS policies, without sharing sessions across
threads or using an administrative credential to bypass those policies.

Initial writes use INSERT with a deterministic primary key. Later writes match
both the user ID and current JSON payload version in one UPDATE. A stale save
returns 409 and asks for a reload. A failed load produces `storageError`, rather
than silently presenting an empty account that can overwrite saved data. The
UI blocks saves until a successful reload. Successful saves include `user`,
`data`, and `version`, so they preserve the signed-in frontend state.

Deployment requires the team's existing Supabase URL/key configuration and
the `saved_plans` schema/policies above. Expired or rejected access tokens ask
the user to sign in again. Server logs record only the error class and provider
code; provider messages, tokens, and saved data are not logged or exposed.

Validation uses the real Supabase Python query builder with an offline HTTP
transport that checks user tokens, RLS isolation, schema fields, and JSON
version filters. It also exercises the actual Python HTTP save/reload routes
and frontend state handling. A real production account still needs a save and
refresh check after deployment; local Supabase credentials are not configured.

Reference: [Supabase row-level security](https://supabase.com/docs/guides/database/postgres/row-level-security).
