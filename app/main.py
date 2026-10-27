"""Feed API: FastAPI + asyncpg (one pool per worker), served by Gunicorn with uvicorn workers (gunicorn.conf.py)."""
import time
from contextlib import asynccontextmanager
from typing import Annotated

import asyncpg
from fastapi import Depends, FastAPI, Path, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from .auth import CurrentUser
from .db import (ACQUIRE_TIMEOUT_S, FEED_SIZE, FEED_SQL, INSERT_LIKE_SQL, INSERT_POST_SQL, POST_SQL,
                 create_pool)
from .errors import ApiError, install_error_handlers
from .models import FeedResponse, LikeResponse, NewPost, Post, PostResponse

STARTED_AT = time.monotonic()
MAX_BODY_LENGTH = 500


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once in each Gunicorn worker (UvicornWorker runs the ASGI lifespan), so each worker
    # process owns its own pool on its own event loop.
    app.state.pool = await create_pool()
    yield
    await app.state.pool.close()


# Production: no /docs, /redoc or /openapi.json (unknown paths must be 404).
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware)  # defaults: minimum_size=500 bytes, compresslevel=9
install_error_handlers(app)


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


DbPool = Annotated[asyncpg.Pool, Depends(get_pool)]
PostId = Annotated[int, Path(gt=0)]


def to_post(row: asyncpg.Record) -> Post:
    return Post(id=row[0], body=row[1], created_at=row[2], author=row[3], like_count=row[4])


@app.get("/health")
async def health(pool: DbPool):
    try:
        async with pool.acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        return JSONResponse({"status": "degraded", "db": "unreachable", "error": str(exc)}, status_code=503)
    return {"status": "ok", "db": "ok", "uptime_s": int(time.monotonic() - STARTED_AT)}


@app.get("/feed")
async def feed(pool: DbPool) -> FeedResponse:
    async with pool.acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
        rows = await conn.fetch(FEED_SQL, FEED_SIZE)
    return FeedResponse(posts=[to_post(r) for r in rows])


@app.get("/posts/{post_id}")
async def get_post(post_id: PostId, pool: DbPool) -> PostResponse:
    async with pool.acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
        row = await conn.fetchrow(POST_SQL, post_id)
    if row is None:
        raise ApiError(404, "post not found")
    return PostResponse(post=to_post(row))


@app.post("/posts", status_code=201)
async def create_post(user: CurrentUser, new_post: NewPost, pool: DbPool) -> PostResponse:
    body = new_post.body.strip()
    if not body:
        raise ApiError(400, "body is required")
    if len(body) > MAX_BODY_LENGTH:
        raise ApiError(400, f"body must be at most {MAX_BODY_LENGTH} characters")
    async with pool.acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
        post_id, created_at = await conn.fetchrow(INSERT_POST_SQL, user.id, body)
    return PostResponse(post=Post(id=post_id, body=body, created_at=created_at, author=user.username, like_count=0))


# The auth dependency is declared first: FastAPI resolves dependencies before validating the
# path parameter, so a missing token is a 401 even when the id is invalid.
@app.post("/posts/{post_id}/like")
async def like_post(user: CurrentUser, post_id: PostId, pool: DbPool, response: Response) -> LikeResponse:
    try:
        async with pool.acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
            status = await conn.execute(INSERT_LIKE_SQL, user.id, post_id)
    except asyncpg.ForeignKeyViolationError:  # SQLSTATE 23503: no such post
        raise ApiError(404, "post not found")
    inserted = status == "INSERT 0 1"  # command tag: rows affected 1 or 0 (ON CONFLICT DO NOTHING)
    response.status_code = 201 if inserted else 200
    return LikeResponse(liked=True, already_liked=not inserted, post_id=post_id)
