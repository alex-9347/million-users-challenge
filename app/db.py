"""Postgres with asyncpg: one asyncpg pool per Gunicorn worker process, created in the lifespan handler.

Pool size per worker: DB_POOL if set, else max(2, 128 // workers). With Gunicorn's 2 x cores + 1
workers the whole app stays at about 128 server connections or fewer (see README.md for the table).
"""
import os

import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]
ACQUIRE_TIMEOUT_S = 5.0
FEED_SIZE = 20

POST_SELECT = """SELECT p.id, p.body, p.created_at, u.username,
       (SELECT count(*) FROM likes l WHERE l.post_id = p.id)::int AS like_count
  FROM posts p
  JOIN users u ON u.id = p.user_id"""
FEED_SQL = POST_SELECT + " ORDER BY p.created_at DESC, p.id DESC LIMIT $1"
POST_SQL = POST_SELECT + " WHERE p.id = $1"
INSERT_POST_SQL = "INSERT INTO posts (user_id, body) VALUES ($1, $2) RETURNING id, created_at"
INSERT_LIKE_SQL = "INSERT INTO likes (user_id, post_id) VALUES ($1, $2) ON CONFLICT (user_id, post_id) DO NOTHING"


def pool_size() -> int:
    if os.environ.get("DB_POOL"):
        return int(os.environ["DB_POOL"])
    # Gunicorn exports nothing about the worker count, so it is recomputed exactly as in
    # gunicorn.conf.py (WEB_CONCURRENCY, else 2 x affinity cores + 1).
    affinity = getattr(os, "sched_getaffinity", None)
    cores = len(affinity(0)) if affinity else (os.cpu_count() or 1)
    workers = int(os.environ.get("WEB_CONCURRENCY") or 2 * cores + 1)
    return max(2, 128 // workers)


async def create_pool() -> asyncpg.Pool:
    size = pool_size()
    # min_size = max_size: every connection is opened at worker start, none is closed when idle
    # (max_inactive_connection_lifetime=0), so a burst never waits on a TCP + SCRAM handshake.
    return await asyncpg.create_pool(DATABASE_URL, min_size=size, max_size=size,
                                     max_inactive_connection_lifetime=0)
