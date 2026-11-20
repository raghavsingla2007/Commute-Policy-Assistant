"""Pure ASGI middleware that wraps every HTTP request:

- X-Request-ID: echo the client's value only if it is safe ([A-Za-z0-9._-], max 64 chars), otherwise
  generate one. It is used for logs and echoed in the response; it is never used for any decision.
- Body size limit: 413 if Content-Length is over the limit, or if a streamed body grows past it.
- Catch-all: an unhandled exception becomes a plain 503 "try again" (traceback logged server-side
  with the request id). No stack trace or internal detail ever reaches the client.

It reads exactly two request headers, x-request-id and content-length, straight from the ASGI scope
(tests/test_principal_only.py checks this). It never reads the body's content or Authorization.
"""

import json
import logging
import re
import uuid

from fastapi import HTTPException

from app import messages

log = logging.getLogger("app.http")

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ADMIN_BODY_LIMIT = 256 * 1024        # a policy document (MAX_POLICY_CHARS = 200,000 chars) fits
DEFAULT_BODY_LIMIT = 16 * 1024       # /ask: a 500-character question needs far less
RETRY_AFTER_S = 2


class BodyTooLarge(HTTPException):
    """Raised from receive() while FastAPI reads the body; FastAPI re-raises HTTPExceptions as-is."""

    def __init__(self) -> None:
        super().__init__(status_code=413)


def body_limit(path: str) -> int:
    return ADMIN_BODY_LIMIT if path.startswith("/admin/") else DEFAULT_BODY_LIMIT


def sanitise_request_id(raw: bytes | None) -> str:
    value = raw.decode("latin-1") if raw else ""
    return value if _SAFE_ID.fullmatch(value) else uuid.uuid4().hex   # fullmatch: "$" alone accepts a trailing \n


def json_message(status: int, body: dict, request_id: str, extra_headers: list | None = None) -> tuple[dict, dict]:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
               (b"x-request-id", request_id.encode())] + (extra_headers or [])
    return ({"type": "http.response.start", "status": status, "headers": headers},
            {"type": "http.response.body", "body": payload})


class RequestContextMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        wanted = {b"x-request-id": None, b"content-length": None}
        for name, value in scope["headers"]:
            if name in wanted:
                wanted[name] = value
        request_id = sanitise_request_id(wanted[b"x-request-id"])
        scope.setdefault("state", {})["request_id"] = request_id
        path, limit = scope["path"], body_limit(scope["path"])

        async def reply(status: int, body: dict, extra_headers: list | None = None) -> None:
            start, chunk = json_message(status, {**body, "request_id": request_id}, request_id, extra_headers)
            await send(start)
            await send(chunk)

        declared = wanted[b"content-length"]
        if declared is not None and declared.isdigit() and int(declared) > limit:
            log.warning("413 request_id=%s path=%s content-length=%s", request_id, path, int(declared))
            return await reply(413, {"status": "payload_too_large", "message": messages.PAYLOAD_TOO_LARGE})

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        started = False

        async def send_with_id(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = {**message, "headers": list(message.get("headers", [])) + [(b"x-request-id", request_id.encode())]}
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_id)
        except Exception:
            log.exception("unhandled error request_id=%s path=%s", request_id, path)
            if not started:
                await reply(503, {"status": "unavailable", "message": messages.UNAVAILABLE},
                            [(b"retry-after", str(RETRY_AFTER_S).encode())])
