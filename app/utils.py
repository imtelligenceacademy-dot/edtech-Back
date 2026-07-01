from __future__ import annotations

import secrets

from fastapi import Request


def new_id(prefix: str) -> str:
    """Short, collision-resistant, human-greppable id, e.g. 'u_3f9 a1...'."""
    return f"{prefix}_{secrets.token_hex(8)}"


def client_ip(request: Request) -> str:
    # Honour a single proxy hop if present, else the socket peer.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:300]
