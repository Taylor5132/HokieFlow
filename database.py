import os
from typing import Optional

# python-dotenv is OPTIONAL: app/server.py already loads the same .env through
# app/local_env.py (stdlib) before anything else imports this module, so the
# value here is only convenience for someone importing database.py directly.
try:
    from dotenv import load_dotenv
except ImportError:                                            # pragma: no cover
    load_dotenv = None

from supabase import Client, create_client

if load_dotenv is not None:
    load_dotenv()


def get_supabase_url() -> Optional[str]:
    return os.getenv("SUPABASE_URL")


def get_supabase_key() -> Optional[str]:
    return os.getenv("SUPABASE_KEY")


def get_supabase_client() -> Client:
    url = get_supabase_url()
    key = get_supabase_key()
    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE_URL or SUPABASE_KEY. Set them in the environment or .env."
        )
    return create_client(url, key)


supabase: Optional[Client] = None
try:
    supabase = get_supabase_client()
except RuntimeError:
    supabase = None