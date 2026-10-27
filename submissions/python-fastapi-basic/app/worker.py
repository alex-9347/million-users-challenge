from uvicorn_worker import UvicornWorker


class Worker(UvicornWorker):
    """UvicornWorker with uvloop + httptools named explicitly (what "auto" picks when
    uvicorn[standard] is installed) and uvicorn's access log off. Subclassing CONFIG_KWARGS is the
    documented way to pass uvicorn settings that gunicorn has no flag for."""

    CONFIG_KWARGS = {"loop": "uvloop", "http": "httptools", "access_log": False}
