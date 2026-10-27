from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        # Router errors: unknown path -> 404 "not found", wrong method -> 405.
        return JSONResponse({"error": str(exc.detail).lower()}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        if any(e["loc"][0] == "path" for e in errors):
            message = "invalid post id"
        elif any(e["type"] == "json_invalid" for e in errors):
            message = "malformed JSON body"
        else:
            message = "body is required"
        return JSONResponse({"error": message}, status_code=400)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        return JSONResponse({"error": "internal server error"}, status_code=500)
