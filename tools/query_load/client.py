"""HTTP client retaining every D0 response and failure for Gate 0."""

from __future__ import annotations

import base64
import json
import math
import time
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from .accounting import ClientRecord, RunAccounting


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _invalid_constant(_text: str) -> None:
    raise ValueError("non-finite JSON constant")


class QueryClient:
    """One client with a fixed base URL, token and workload user label."""

    def __init__(
        self,
        base_url: str,
        token: str,
        user_id: str,
        accounting: RunAccounting,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._user_id = user_id
        self._accounting = accounting
        self._timeout = timeout_seconds
        # Loopback evidence must describe the direct client/server stream,
        # regardless of environment, system proxies or an installed opener.
        self._opener = build_opener(ProxyHandler({}))

    def query(
        self,
        run_id: str,
        device_id: str,
        *,
        page_size: int = 50,
        windows_cursor: str | None = "",
        alarms_cursor: str | None = "",
        streams: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        """Issue a real query, retaining raw response bytes and safe error codes.

        Neither request headers nor exception messages are stored: they may
        contain a bearer token or a URL.  Transport failures use status zero;
        HTTP failures retain the real status and body.  Every started call is
        completed in the accounting even when reading or decoding fails.
        """
        url = f"{self._base_url}/v1/runs/{run_id}/devices/{device_id}/health"
        params = []
        if streams is not None:
            params.append(f"streams={quote(streams)}")
        if page_size != 50:
            params.append(f"page_size={page_size}")
        if windows_cursor:
            params.append(f"windows_cursor={quote(windows_cursor)}")
        if alarms_cursor:
            params.append(f"alarms_cursor={quote(alarms_cursor)}")
        if params:
            url += "?" + "&".join(params)
        request = Request(url, headers={"Authorization": f"Bearer {self._token}"})
        status = 0
        raw = b""
        body: dict[str, object] = {}
        error = ""
        started = self._accounting.client_start()
        waiting = time.perf_counter()
        try:
            try:
                try:
                    with self._opener.open(request, timeout=self._timeout) as response:
                        status = response.status
                        raw = response.read()
                except HTTPError as exc:
                    status = exc.code
                    with exc:
                        raw = exc.read()
            except (URLError, OSError, HTTPException) as exc:
                status = 0
                error = type(exc).__name__
                if isinstance(exc, IncompleteRead):
                    raw = exc.partial
            if not error:
                try:
                    decoded = json.loads(
                        raw.decode("utf-8"),
                        parse_float=_finite_float,
                        parse_constant=_invalid_constant,
                    )
                    if isinstance(decoded, dict):
                        body = decoded
                    else:
                        error = "non_object_json"
                except (ValueError, UnicodeDecodeError):
                    error = "invalid_json"
            if not error and status != 200:
                error = f"http_status_{status}"
        finally:
            completed = time.perf_counter()
            request_id = body.get("request_id")
            trace_id = body.get("trace_id")
            self._accounting.client_complete(
                ClientRecord(
                    user_id=self._user_id,
                    run_id=run_id,
                    device_id=device_id,
                    started_monotonic=started,
                    waiting_monotonic=waiting,
                    completed_monotonic=completed,
                    status=status,
                    server_request_id=(
                        request_id if isinstance(request_id, str) else ""
                    ),
                    trace_id=trace_id if isinstance(trace_id, str) else "",
                    error=error,
                    response_bytes_base64=base64.b64encode(raw).decode("ascii"),
                )
            )
        return status, body
