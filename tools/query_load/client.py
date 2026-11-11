"""HTTP client driver for the Gate 0 load harness.

Each user makes one real HTTP request per call.  The client records
client_started / client_waiting / client_completed around the actual socket
work and extracts the server request_id from the response body, so the
accounting layer can tie every client record to the server event stream.
"""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .accounting import RunAccounting, ClientRecord


class QueryClient:
    """One client with a fixed base URL and bearer token."""

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

    def query(
        self,
        run_id: str,
        device_id: str,
        *,
        page_size: int = 50,
        windows_cursor: str = "",
        alarms_cursor: str = "",
    ) -> tuple[int, dict[str, object]]:
        """Issue one real query and record trusted client-side accounting."""
        url = f"{self._base_url}/v1/runs/{run_id}/devices/{device_id}/health"
        params = []
        if page_size != 50:
            params.append(f"page_size={page_size}")
        if windows_cursor:
            params.append(f"windows_cursor={quote(windows_cursor)}")
        if alarms_cursor:
            params.append(f"alarms_cursor={quote(alarms_cursor)}")
        if params:
            url += "?" + "&".join(params)
        request = Request(url, headers={"Authorization": f"Bearer {self._token}"})
        self._accounting.client_start()
        started = time.monotonic()
        waiting = time.monotonic()
        status = 0
        try:
            with urlopen(request, timeout=self._timeout) as response:
                status = response.status
                raw = response.read()
        except HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except URLError:
            status = 0
            raw = b"{}"
        completed = time.monotonic()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            body = {}
        server_request_id = str(body.get("request_id", ""))
        self._accounting.client_complete(
            ClientRecord(
                user_id=self._user_id,
                run_id=run_id,
                device_id=device_id,
                started_monotonic=started,
                waiting_monotonic=waiting,
                completed_monotonic=completed,
                status=status,
                server_request_id=server_request_id,
            )
        )
        return status, body
