import os

from dotenv import load_dotenv
from supabase import create_client, Client

# Load variables from .env
load_dotenv()

# Get Supabase credentials
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Create Supabase connection
supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)