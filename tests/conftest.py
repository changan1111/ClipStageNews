import os
import sys
from pathlib import Path

os.environ.setdefault("SEARCH_BACKEND", "typesense")
os.environ.setdefault("TYPESENSE_KEY", "test-key")
os.environ.setdefault("SUPABASE_URL", "http://supabase.invalid")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
