from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, PlainSerializer


def _iso_millis(value: datetime) -> str:
    """TIMESTAMPTZ (asyncpg gives an aware datetime) -> UTC, ms TRUNCATED like JavaScript's Date, 'Z'."""
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


UtcMillis = Annotated[datetime, PlainSerializer(_iso_millis, return_type=str)]


# Field order is the wire key order.
class Post(BaseModel):
    id: int
    body: str
    created_at: UtcMillis
    author: str
    like_count: int


class PostResponse(BaseModel):
    post: Post


class FeedResponse(BaseModel):
    posts: list[Post]


class LikeResponse(BaseModel):
    liked: bool
    already_liked: bool
    post_id: int


class NewPost(BaseModel):
    body: str  # pydantic v2 does not coerce numbers to str, so {"body":123} is a validation error
