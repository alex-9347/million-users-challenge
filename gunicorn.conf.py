# Gunicorn settings (https://docs.gunicorn.org/en/stable/settings.html). HOST/PORT/EXTRA_PORT from the environment.
import os

_host = os.environ.get('HOST', '127.0.0.1')
# A list binds every address with the same settings and the same workers. EXTRA_PORT is a second
# server port (one port only reaches ~770k client connections from 3 load generators x 4 IPs).
bind = [f"{_host}:{os.environ.get('PORT', '3000')}"]
if os.environ.get("EXTRA_PORT"):
    bind.append(f"{_host}:{os.environ['EXTRA_PORT']}")

# Gunicorn's documented starting point: (2 x cores) + 1. Cores = the CPUs this process may run on
# (systemd CPUAffinity / taskset), not os.cpu_count(), which reports every CPU in the machine.
affinity = getattr(os, "sched_getaffinity", None)
cores = len(affinity(0)) if affinity else (os.cpu_count() or 1)
workers = int(os.environ.get("WEB_CONCURRENCY") or 2 * cores + 1)  # app/db.py uses the same formula
worker_class = "app.worker.Worker"

# Each worker binds its own SO_REUSEPORT listening socket (Gunicorn creates them per worker when
# this is on), so the kernel spreads new connections evenly across workers. With the default
# shared socket, a few workers accept most connections (measured: 2.7-3.5x the median worker's
# connections and CPU), and the busiest worker hits 100% of a core long before the others.
reuse_port = True

# Clients idle up to ~40 s between requests on one keep-alive connection. UvicornWorker passes
# this to uvicorn as timeout_keep_alive (uvicorn's own default is 5 s, gunicorn's 2 s).
keepalive = 75
backlog = 65535  # default 2048; the kernel caps it at net.core.somaxconn
timeout = 60     # a worker silent for 60 s is restarted (default 30)
max_requests = 0  # default: workers are never recycled
accesslog = None  # default: no access log
errorlog = "-"
loglevel = "warning"
