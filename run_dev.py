"""Local development runner for Windows / local environments."""
import os
import sys

# Set defaults if not provided in environment
os.environ.setdefault("JWT_SECRET", "million-users-challenge")
os.environ.setdefault("HOST", "127.0.0.1")
os.environ.setdefault("PORT", "3000")

if "DATABASE_URL" not in os.environ:
    print("[WARNING] DATABASE_URL is not set!")
    print("Example: set DATABASE_URL=postgres://user:password@localhost:5432/feed")
    print("Defaulting to: postgres://feed:feed@localhost:5432/feed\n")
    os.environ["DATABASE_URL"] = "postgres://feed:feed@localhost:5432/feed"

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "3000"))
    print(f"Starting server on http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
