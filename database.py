import os
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

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