import os
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, ClientOptions, create_client
import httpx

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
    # HTTP/1.1 avoids upstream HTTP/2 StreamReset failures during signup.
    return create_client(url, key, options=ClientOptions(
        httpx_client=httpx.Client(http2=False, timeout=15.0),
        auto_refresh_token=False, persist_session=False,
    ))


supabase: Optional[Client] = None
try:
    supabase = get_supabase_client()
except RuntimeError:
    supabase = None