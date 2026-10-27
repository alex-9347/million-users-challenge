# std-python-pg: FastAPI + Gunicorn/uvicorn + asyncpg (Postgres)

This is the Postgres version of `std-python`: the reviewed SQLite app, with only the data layer
swapped (stdlib `sqlite3` replaced by an `asyncpg` pool per worker) and a second listen port added.
These are unchanged: routes, models, auth, error handlers, GZip middleware, the Gunicorn/uvicorn
settings and the worker count. The settings below are the ones the std-python review checked.

## Stack

| | |
|---|---|
| Language | Python 3.12.3 (Ubuntu 24.04 `python3`, plus `python3-venv`) |
| Framework | FastAPI 0.143.3 (Starlette 1.8.0, Pydantic 2.14.1) |
| Server | Gunicorn 26.2.0 + uvicorn 0.54.0 via `uvicorn-worker` 0.4.0 (`UvicornWorker`) |
| Event loop / HTTP parser | uvloop 0.23.0 / httptools 0.9.0 (from `uvicorn[standard]`) |
| Postgres driver | asyncpg 0.32.0 and its built-in pool (`asyncpg.create_pool`), async endpoints |
| Database | PostgreSQL 18.6 (tested on the test box) |
| JWT | PyJWT 2.15.1 |
| JSON | FastAPI default: Pydantic serializes the response models, and Starlette's `JSONResponse` writes the error bodies (compact JSON) |

`requirements.txt` pins every package, transitive ones included. asyncpg has no dependencies on Python 3.12.

```bash
sudo bash install.sh   # python3 + python3-venv, only if missing
bash build.sh          # .venv + pinned requirements (idempotent)
DATABASE_URL=postgres://user:pass@host:5432/db JWT_SECRET=... HOST=0.0.0.0 PORT=80 EXTRA_PORT=81 bash start.sh
```

Env: `DATABASE_URL`, `JWT_SECRET`, `HOST`, `PORT`, optional `EXTRA_PORT` (second port), optional
`DB_POOL` (pool size per worker process), optional `WEB_CONCURRENCY` (worker count).

## Process, thread and connection model

- **Processes:** a Gunicorn master and `2 x cores + 1` uvicorn workers. This is Gunicorn's
  documented starting point. `cores` is `len(os.sched_getaffinity(0))`, so it follows systemd's
  `CPUAffinity`/`AllowedCPUs` or `taskset` (1c: 3 workers, 4c: 9, 16c: 33). It does not use
  `os.cpu_count()`, which reports all 16 CPUs whatever the affinity. `WEB_CONCURRENCY` overrides it.
  `reuse_port = True`: each worker binds its own `SO_REUSEPORT` listening socket for each address,
  and the kernel spreads connections across them (see "Why `reuse_port`" below).
- **Threads:** each worker runs a single uvloop event loop. Endpoints are `async def` and `await`
  asyncpg, so a worker keeps serving other connections while a query is on its way to Postgres.
- **DB pool:** one `asyncpg` pool per worker process. It is created in FastAPI's `lifespan` handler,
  which `UvicornWorker` runs in each worker after the fork, on that worker's loop, and it is closed
  on shutdown. Every call is `pool.acquire(timeout=5)`, so the acquire timeout is 5 s. Pool size per worker:

  `DB_POOL` if set, else `max(2, 128 // workers)`. `min_size = max_size`, so every connection is
  opened at worker start. `max_inactive_connection_lifetime=0`: asyncpg's default closes a
  connection after 300 s idle, and the next burst would then wait on new TCP + SCRAM handshakes.

  | Cores | Workers | Per worker | Total Postgres connections |
  |---|---|---|---|
  | 1 | 3 | 42 | 126 |
  | 2 | 5 | 25 | 125 |
  | 4 | 9 | 14 | 126 (checked in `pg_stat_activity`) |
  | 8 | 17 | 7 | 119 |
  | 16 | 33 | 3 | 99 |

  The formula was first `max(2, 64 // workers)` (66 connections at 16 cores, 2 per worker). It was
  raised to `128 // workers` for all the Postgres variants after the Go review measured that one
  connection only serves about 600-1,600 req/s (each query waits a network round trip), so a pool
  of 64 can cap the app below the ~42k req/s that 1M users send. Postgres allows 300 connections.

  Pool size per worker measured (review, the test box, server `taskset -c 8-11` = 9 workers, fresh DB,
  mload 10,000 users on both ports, `-think-scale 0.25` (about 4,200 req/s offered, above what 4
  cores serve), 15 s ramp + 45 s hold; other agents were loading the same cores and Postgres, so
  runs vary by about ±10%):

  | Per worker | Total | Runs: req/s / p95 / err |
  |---|---|---|
  | 1 | 9 | 2,671 / 2.5 s / 0.27%, 3,295 / 1.2 s / 0.001% |
  | 2 | 18 | 3,056 / 2.0 s / 0.09%, 3,404 / 1.1 s / 0.003% |
  | 4 | 36 | 3,420 / 1.4 s / 0.16%, 3,281 / 2.0 s / 0.55% |
  | 14 (default) | 126 | 2,953 / 3.6 s / 2.0%, 2,765 / 5.2 s / 6.2%, 2,752 / 5.9 s / 6.6%, 3,535 / 0.87 s / 0.02% |

  No pool size wins consistently: the spread between repeats of the same size is as large as the
  spread between sizes, and it follows how busy the other agents kept the shared cores. A worker
  is limited by its own CPU (one Python event loop: about 380 req/s per worker, ~850 per core
  here), not by connections; even 1 connection per worker, i.e. 1 query in flight per worker, was
  not the limit at 4 cores. 1M users at 16 cores would need ~1,270 req/s per worker, which is
  more than one Python worker serves, while 3 connections per worker can carry ~1,800-4,800. So the
  default formula stays as the brief gives it. In overload the
  errors are `pool.acquire` timeouts (5 s), returned as 500 JSON; nothing crashes.
  `app/db.py` recomputes the worker count with the same expression as `gunicorn.conf.py`, because
  Gunicorn does not tell the app how many workers it started.
- **SQL:** the spec's Postgres reference queries, verbatim (`$1` placeholders, `::int` on
  `like_count`), one statement per request, in autocommit (asyncpg runs every statement outside a
  transaction unless you open one). asyncpg prepares each statement once per connection and caches
  it (its default statement cache). `POST /posts` reads the `INSERT ... RETURNING` with `fetchrow`.
  A like on a missing post raises `asyncpg.ForeignKeyViolationError` (SQLSTATE 23503), which
  becomes 404 `post not found`. Whether the row was inserted comes from the command tag:
  `INSERT 0 1` is 201, `INSERT 0 0` is 200.
- **`created_at`:** formatted in the app. asyncpg returns `TIMESTAMPTZ` as an aware `datetime`.
  A Pydantic `PlainSerializer` on `Post.created_at` converts it to UTC and writes
  `YYYY-MM-DDTHH:MM:SS.mmmZ` using `microsecond // 1000`, which truncates instead of rounding. The
  older `servers/python` Postgres version used the same formatter.
- **Second port:** Gunicorn's `bind` is a list: `[HOST:PORT]`, plus `HOST:EXTRA_PORT` when
  `EXTRA_PORT` is set. Both addresses get the same master, the same workers and the same settings.
  With `reuse_port`, every worker opens its own listening socket on each address. At 16 cores that
  is 33 x 2 = 66 sockets; on 4 cores I checked 9 workers and 18 `LISTEN` sockets.

### Why there is no threadpool

The SQLite version compared `async def` endpoints that make blocking calls inline against `def`
endpoints run in FastAPI's threadpool. With asyncpg that choice does not come up: the driver is
natively async, so `async def` + `await` is the documented FastAPI pattern and no threads are involved.

### Why `reuse_port` (measured in the std-python review on the SQLite version; the HTTP side is unchanged)

With Gunicorn's default, the master binds one socket and every worker accepts from it, so
whichever worker happens to be waiting wins the connection. Measured on the test box (fresh DB, mload
`-gzip`, 10,000 users, server `taskset -c 12-13` = 5 workers, mload on two other cores):

| think-scale | Mode | req/s | p95 | p99 | Connections per worker (mid-run) | Worker CPU, busiest / least |
|---|---|---|---|---|---|---|
| 0.25 | shared socket | 3,202 | 36.4 ms | 349 ms | 3237 / 2428 / 1523 / 1457 / 1455 | 42 s / 16 s |
| 0.25 | `reuse_port` | 3,205 | 16.8 ms | 374 ms | 2054 / 2028 / 2024 / 2020 / 1974 | 24 s / 23 s |
| 0.25 (repeat) | shared socket | 3,181 | 65.5 ms | 750 ms | 3292 ... 1404 | 42 s / 15 s |
| 0.25 (repeat) | `reuse_port` | 3,201 | 21.9 ms | 504 ms | 2110 ... 1953 | 24 s / 23 s |
| 0.2 (saturated) | shared socket | 3,565 | 820 ms | 1,209 ms | | 38 s / 21 s |
| 0.2 (saturated) | `reuse_port` | 3,623 | 602 ms | 1,105 ms | | 29 s / 26 s |

On 4 cores (9 workers, 120,000 users, think-scale 2.5) the shared socket gave the busiest worker
26,488 connections and the median one 7,692. One worker can use at most one core, so the busiest
worker sets the ceiling, and that gets worse as the worker count grows (33 workers at 16 cores).
With `reuse_port` the spread is within ±4%. The 16-core ceiling was not measured in this review.

One side effect: with `SO_REUSEPORT` a second copy of the server started on the same port binds
fine and silently shares the traffic, instead of failing with `Address already in use`. Stop the
old instance first (systemd does).

### Workers: why `2 x cores + 1` and not `cores` (same review)

Gunicorn's formula is written for sync workers, but the extra processes still help here: a worker
busy with a burst cannot preempt itself, while the kernel can switch between several workers on one
core. Measured with `reuse_port`, 10,000 users:

| Cores | Workers | think-scale | req/s | p95 | p99 |
|---|---|---|---|---|---|
| 2 | 5 (2n+1) | 0.25 | 3,205 | 16.8 ms | 374 ms |
| 2 | 2 (n) | 0.25 | 3,160 | 194 ms | 639 ms |
| 1 | 3 (2n+1) | 0.6 | 1,366 | 9.7 ms | 303 ms |
| 1 | 3, shared socket | 0.6 | 1,369 | 8.7 ms | 98.5 ms |
| 1 | 2 | 0.6 | 1,366 | 29.0 ms | 160 ms |
| 1 | 1 (n) | 0.6 | 1,357 | 91.9 ms | 192 ms |

The ceiling is the same either way (about 1,950 req/s per core); 2n+1 gives the lowest p95, so
it stays, including 3 workers on 1 core. At 1 core `reuse_port` makes no difference.

## Non-default settings, and why

`gunicorn.conf.py`:

| Setting | Value | Default | Why |
|---|---|---|---|
| `workers` | `2 x affinity cores + 1` | 1 | Gunicorn's documented formula. Uses every core it is given |
| `worker_class` | `app.worker.Worker` (subclass of `uvicorn_worker.UvicornWorker`) | sync | ASGI app. Subclassing `CONFIG_KWARGS` is the documented way to pass uvicorn settings |
| `reuse_port` | True | False | One listening socket per worker, balanced by the kernel. The shared socket left a few workers with most of the connections (see "Why `reuse_port`") |
| `keepalive` | 75 s | 2 s (uvicorn alone: 5 s) | Clients idle up to ~40 s between requests. `UvicornWorker` passes this to uvicorn as `timeout_keep_alive` |
| `backlog` | 65535 | 2048 | Accept bursts while 1M connections ramp up. Capped by `net.core.somaxconn` |
| `timeout` | 60 s | 30 s | Worker heartbeat. A loop busy with ~30k connections per worker should not be killed for a short stall; killing one drops all its connections |
| `loglevel` / `errorlog` | `warning` / stderr | `info` / stderr | Production: no per-worker boot chatter |
| `max_requests` | 0 | 0 | Stated explicitly: workers are never recycled |
| `accesslog` | None | None | Stated explicitly: no access log |

`app/worker.py`: `CONFIG_KWARGS = {"loop": "uvloop", "http": "httptools", "access_log": False}`.
uvloop and httptools are what `auto` picks anyway; they are named explicitly. `access_log=False`
turns off uvicorn's access logger, whatever Gunicorn's logging is set to. uvicorn's
`limit_concurrency` stays unset (no cap), and `limit_max_requests` stays off. Gunicorn's
`worker_connections` does not apply to this worker class.

`app/main.py`:
- `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`: production. It also makes
  `/docs` a 404 like any other unknown path.
- `GZipMiddleware` with Starlette's defaults (`minimum_size=500`, `compresslevel=9`). `/feed`
  goes out gzipped when the client sends `Accept-Encoding: gzip`. Small bodies and clients that
  don't ask for gzip get plain JSON.
- Exception handlers map everything to the spec's `{"error": ...}` bodies.
  `RequestValidationError` on the path becomes `invalid post id`, `json_invalid` becomes
  `malformed JSON body`, and any other body error becomes `body is required`. Router 404/405 come
  out lower-cased (`not found`), and any other exception is a 500 `internal server error`.
- The auth dependency comes first in the like endpoint's signature. FastAPI resolves dependencies
  before it validates path parameters, so `POST /posts/abc/like` with no token is a 401, as the
  spec requires.

`app/auth.py`: `jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp"]})`
checks the signature and `exp`, and accepts HS256 only. PyJWT accepts the 23-byte challenge secret
(`enforce_minimum_key_length` is off by default) but warns about it on every decode. The key is
fixed by the spec, so `InsecureKeyLengthWarning` is filtered out once at import.

Other checks from the review:
- `timeout = 60`: `UvicornWorker` gets `timeout / 2` = 30 s and uvicorn's 0.1 s tick heartbeats
  every 30 s from the event loop, so a worker is only killed if its loop is stuck for 30-60 s. A
  2x overload on 1 core for 3 minutes (10,000 users, think-scale 0.2) killed no worker: same PIDs
  before and after, no `WORKER TIMEOUT` in the log.
- Keep-alive: 2,000 idle connections each survived 40 s and 70 s idle and served a second request;
  after 80 s all were closed by the server (uvicorn's `timeout_keep_alive` = 75, armed after each
  response).


Postgres-specific settings (`app/db.py`, `gunicorn.conf.py`):

| Setting | Value | Default | Why |
|---|---|---|---|
| `bind` | list: `HOST:PORT` and `HOST:EXTRA_PORT` | one address | Second server port: one port only reaches ~770k client connections (3 load generators x 4 IPs x ~64k source ports) |
| pool `min_size` / `max_size` | `DB_POOL` or `max(2, 128 // workers)`, both | 10 / 10 | Keeps the whole app at 128 Postgres connections or fewer (see the table above) |
| `max_inactive_connection_lifetime` | 0 (never close idle) | 300 s | A fixed pool; no reconnect storm after a quiet period |
| `pool.acquire(timeout=...)` | 5 s | none (wait forever) | The brief's acquire timeout. A request that cannot get a connection in 5 s becomes a 500 instead of hanging |

## Results on the test box

Server under `taskset -c 8-11` (4 cores, 9 workers, pool 14 each, 126 connections in `pg_stat_activity`),
Postgres 18.6 on the same box, `HOST=127.0.3.1 PORT=3931 EXTRA_PORT=3932` (review run), a fresh
database from `feed_seed` before each run:

- `test.sh http://127.0.3.1:3931`: `passed=42 failed=0`
- `test.sh http://127.0.3.1:3932` (another fresh copy): `passed=42 failed=0`
- Smoke, both ports: `RESULT users=3000 req/s=127 p95=3.1ms p99=4.3ms err=0.000% pass=true errors=map[]`
- Both ports, 2,000 idle connections (1,000 per port): every worker listens on both, connections
  per worker were 97-130 on each port (9 workers, ~111 average), and all 2,000 served a second
  request after 70-72 s idle.
- 2x overload for 3 minutes (10,000 users, `-think-scale 0.15`): same 9 worker PIDs before and
  after, no `WORKER TIMEOUT`; the failures were `pool.acquire` timeouts returned as 500 JSON.
- `created_at`: 40 fresh posts compared with `to_char(created_at, ... .US)` in Postgres: all
  truncated (e.g. `.140482` -> `.140Z`, sub-ms parts up to `.xxx968` not rounded up).

## Notes for the test bench

- On the test box, starting the server sometimes logged `Connection in use: ('127.0.0.1', 3112)`. Workers
  then exited and were respawned until the bind worked (5-15 s), and some boots were clean. This
  only happened on 3112, never on 3111. Linux `connect()` takes even-numbered ephemeral ports, the
  box's `ip_local_port_range` is 1024-65535, and other agents' clients were caught holding local
  port 3112, for example `127.0.0.1:3112 -> 127.0.0.1:3105`. This cannot happen on ports 80/81,
  which are below the ephemeral range.
- With `SO_REUSEPORT`, a second copy of the server started on the same ports binds fine and silently
  shares the traffic. Stop the old instance first (systemd does).
- Start the server with the open-files limit systemd gives it (`ulimit -n 4194304` matches
  `LimitNOFILE`). An ssh shell's soft limit is 1024, and each worker then stops accepting at
  ~1,000 connections: in the review a 10,000-user run held only 8,991-8,928 connections (9 x ~1,000)
  and the rest saw `EOF` / `connection reset by peer`, with nothing in the server log.
- Each 500 (for example an acquire timeout in overload) logs a traceback at `ERROR`, uvicorn's
  default for unhandled exceptions. That is the error log, not an access log.
