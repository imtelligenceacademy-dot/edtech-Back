from __future__ import annotations

import secrets

from fastapi import Request

from app.config import settings


def new_id(prefix: str) -> str:
    """Short, collision-resistant, human-greppable id, e.g. 'u_3f9 a1...'."""
    return f"{prefix}_{secrets.token_hex(8)}"


def client_ip(request: Request) -> str:
    """The address to hold this request against.

    X-Forwarded-For is a list the client starts and every proxy appends to, so
    its *first* entry is whatever the caller typed. Reading that entry — which
    is what this did — meant login throttling could be side-stepped with a
    fresh fake address per attempt, and a real address could be posted
    deliberately until that network was banned for everyone behind it.

    Only the entries our own proxies appended can be believed, and those are at
    the end. `trusted_proxy_hops` says how many of them there are; with none
    configured the header is ignored and the socket peer stands.
    """
    peer = request.client.host if request.client else ""
    hops = settings.proxy_hops
    if hops <= 0:
        return peer

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not parts:
        return peer
    # The hop-th entry from the right: the address seen by the outermost proxy
    # we trust. A client that pads the header only pads the part we ignore.
    index = len(parts) - hops
    if 0 <= index < len(parts):
        return parts[index]
    # Fewer entries than hops configured, so the header did not come through the
    # chain we were told to expect and none of it can be placed. This fell back
    # to parts[0] — the leftmost, fully client-authored entry, the exact value
    # the docstring above exists to refuse. An attacker short of the configured
    # hop count then chose the address their failures were throttled against and
    # the one written into the security log.
    #
    # Unreachable at the current single-hop deployment, where a non-empty header
    # always yields index 0 or more. It is the day a second proxy is added that
    # this matters, and on that day the socket peer is the only address here
    # that was not supplied by the caller.
    return peer


def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:300]
