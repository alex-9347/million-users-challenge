import os
import warnings
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Header

from .errors import ApiError

JWT_SECRET = os.environ["JWT_SECRET"]
# The challenge secret is 23 bytes; PyJWT accepts it (enforce_minimum_key_length is off by default)
# but warns on every decode. The key is fixed by the spec, so the warning is silenced.
warnings.filterwarnings("ignore", category=jwt.InsecureKeyLengthWarning)


@dataclass(slots=True)
class User:
    id: int
    username: str


async def current_user(authorization: Annotated[str | None, Header()] = None) -> User:
    """`Authorization: Bearer <jwt>`, HS256 only, signature and exp verified by PyJWT."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise ApiError(401, "missing bearer token")
    try:
        payload = jwt.decode(authorization[7:], JWT_SECRET, algorithms=["HS256"],
                             options={"require": ["exp"]})
    except jwt.PyJWTError:
        raise ApiError(401, "invalid or expired token")
    sub, username = payload.get("sub"), payload.get("username")
    user_id = int(sub) if isinstance(sub, str) and sub.isascii() and sub.isdigit() else 0
    if user_id <= 0 or not isinstance(username, str):
        raise ApiError(401, "invalid token payload")
    return User(user_id, username)


CurrentUser = Annotated[User, Depends(current_user)]
