#!/usr/bin/env python3
"""
Clear all translation cache entries (both old v1 and new v2 keys).
This forces all videos to re-translate with the new full-context method.

Run from the backend directory:
    python clear_translation_cache.py
"""

import os
import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

try:
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.ping()
    print("[OK] Connected to Redis")
except Exception as e:
    print(f"[ERROR] Redis connection failed: {e}")
    print("Make sure Redis is running and REDIS_URL is set correctly in .env")
    exit(1)

# Delete all translation cache keys. A single glob covers every version prefix
# (v1, v2, ..., v5, and any future bump) since they all share the
# "translate_paragraphs:" namespace. SCAN avoids blocking Redis on large keyspaces.
pattern = "translate_paragraphs:*"
total_deleted = 0
for key in client.scan_iter(match=pattern, count=500):
    total_deleted += client.delete(key)
print(f"[OK] Deleted {total_deleted} keys matching {pattern}")

print(f"\n[OK] Total keys deleted: {total_deleted}")
print("All saved videos will now re-translate with full context on next access.")
