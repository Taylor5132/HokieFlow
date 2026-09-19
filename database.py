import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = https://kiedafunuugiglokbaad.supabase.co
SUPABASE_ANON_KEY = sb_publishable_l_nx3CKkLuSySPUsgRhaFw_H8FYRa_E

if not SUPABASE_URL:
    raise ValueError("SUPABASE_URL is missing. Add it to your .env file.")

if not SUPABASE_ANON_KEY:
    raise ValueError(
        "SUPABASE_ANON_KEY or SUPABASE_PUBLISHABLE_KEY is missing. Add it to your .env file."
    )

supabase: Client = create_client(https://kiedafunuugiglokbaad.supabase.co, sb_publishable_l_nx3CKkLuSySPUsgRhaFw_H8FYRa_E)