"""Bounded, opt-in D0 observation using one persistent real HTTP process.

The requested load duration is the sum of real concurrent-wave send-to-read-and-
decode spans. Connection preparation, validation and evidence I/O gaps are
reported separately and never filled with sleep or delayed responses.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time
from typing import Any
from urllib.parse import quote, urlsplit

from .accounting import ClientRecord, RunAccounting
from .client import _finite_float, _invalid_constant
from .d0_fixture import D0_DEVICE_COUNT, build_d0_runs, device_id, device_run_id
from .completion_journal import CompletionJournal
from .evidence import query_plans, source_facts
from .event_spool import WaveEventSpool
from .network import MeteredHTTPConnection, SocketNetworkMeter, validate_network_pair
from .oracle import verify_response
from .paging import PagingWalk, run_paging
from .process_runner import _identities, _receive, _validate_resources
from .process_server import EndpointSampler, serve_endpoint
from .runner import _failure, validate_run

MAX_WAVE_BYTES = 16 * 1024 * 1024
DEFAULT_BYTE_BUDGET = 1024 * 1024 * 1024
DEFAULT_MAX_WAVES = 10_000


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(_json_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _file_receipt(path: Path, root: Path, kind: str) -> dict:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {
        "kind": kind,
        "path": path.relative_to(root).as_posix(),
        "sha256": digest.hexdigest(),
        "bytes": size,
    }


def _publication_cost(value: Any, process_id: int, rows: dict) -> tuple[dict, list]:
    """Validate one server clock/PID; never subtract a client timestamp."""
    problems = []
    cost = {
        "wall_seconds": None,
        "cpu_user_seconds": None,
        "cpu_system_seconds": None,
        "cpu_seconds": None,
        "valid": False,
        "scope": "measured server before/after interval: post-handler network snapshot, network/end append, seal/fsync and event IPC; excludes finished control send, final facts hashing, parent validation/hashing and teardown; excluded from request latency/effective load seconds",
        "unmeasured_costs": [
            "finished control send",
            "final server facts hash",
            "parent post-HTTP validation/hashing",
            "teardown",
        ],
    }
    if not isinstance(value, dict):
        return cost, ["publication resources unavailable"]
    before, after = value.get("before"), value.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return cost, ["publication resource endpoints unavailable"]
    if not isinstance(value.get("scope"), str) or not value["scope"]:
        problems.append("publication scope unavailable")
    for sample in (before, after):
        if (
            type(sample.get("process_id")) is not int
            or sample["process_id"] != process_id
            or sample.get("endpoint") != "server_observer_publication"
            or sample.get("clock") != "perf_counter"
        ):
            problems.append("publication resource process/clock mismatch")
    clocks = [sample.get("monotonic_ns") for sample in (before, after)]
    if any(type(clock) is not int or clock <= 0 for clock in clocks):
        problems.append("publication resource clock invalid")
    elif clocks[1] < clocks[0]:
        problems.append("publication resource clock regressed")
    else:
        cost["wall_seconds"] = (clocks[1] - clocks[0]) / 1_000_000_000
        http_clocks = [
            row.get("monotonic_ns")
            for kind in ("transport", "resource")
            for row in rows[kind]
        ]
        if any(type(clock) is not int or clock > clocks[0] for clock in http_clocks):
            problems.append("publication began before HTTP/resource interval ended")
    for key in ("cpu_user_seconds", "cpu_system_seconds"):
        numbers = [sample.get(key) for sample in (before, after)]
        if any(
            type(number) not in (int, float) or not math.isfinite(number) or number < 0
            for number in numbers
        ):
            problems.append("publication CPU measurement invalid")
        elif numbers[1] < numbers[0]:
            problems.append("publication CPU counter regressed")
        else:
            cost[key] = numbers[1] - numbers[0]
    if all(cost[key] is not None for key in ("cpu_user_seconds", "cpu_system_seconds")):
        cost["cpu_seconds"] = cost["cpu_user_seconds"] + cost["cpu_system_seconds"]
    cost["valid"] = not problems
    return cost, problems


class WaveEvidence:
    """One client raw copy and independently receipted durable server frames."""

    def __init__(
        self, root: Path, wave_id: str, *, server_spooled: bool = False
    ) -> None:
        self.wave_id = wave_id
        self.directory = root / wave_id
        self.directory.mkdir()
        self.path = self.directory / "raw.ndjson"
        self.handle = self.path.open("xb")
        self.digest = hashlib.sha256()
        self.bytes = 0
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.end = threading.Event()
        self.rows: dict[str, list] = {
            kind: []
            for kind in (
                "transport",
                "application",
                "resource",
                "network",
                "marker",
                "request_connection",
            )
        }
        self.errors: list[str] = []
        self.closed = False
        self.report = None
        self.client_sampler = None
        self.accounting = None
        self.server_spooled = server_spooled
        self.observer_evidence = None
        self.server_process_id = None
        self.messages: list[dict] = []
        self._receipt = None

    def write(self, kind: str, value: Any, *, origin_wave_id: Any = None) -> None:
        payload = _json_bytes(
            {
                "kind": kind,
                "wave_id": self.wave_id,
                "origin_wave_id": origin_wave_id,
                "value": value,
            }
        )
        with self.lock:
            try:
                self.handle.write(payload)
                self.handle.flush()
            except BaseException as exc:
                self.errors.append("raw_write_" + type(exc).__name__)
                raise
            self.digest.update(payload)
            self.bytes += len(payload)
            self.counts[kind] += 1

    def stream(self, message: dict[str, Any]) -> None:
        # These are the actual delivered envelopes, not reconstructed success.
        if self.server_spooled:
            self.messages.append(message)
        if message.get("wave_id") != self.wave_id:
            self.errors.append("cross-wave event identity")
        kind, row = message.get("kind"), message.get("event")
        if kind not in self.rows or not isinstance(row, dict):
            self.errors.append("invalid event stream record")
            return
        if not self.server_spooled:
            self.write(kind, row, origin_wave_id=message.get("wave_id"))
        self.rows[kind].append(row)
        if kind == "marker" and row.get("marker") == "end":
            self.end.set()

    def _recover_server(self, auxiliary: list) -> dict:
        path = self.directory / f"{self.wave_id}.events.spool"
        recovery_facts, recovered = None, Counter()
        problems = []
        if path.exists():
            try:
                recovery = WaveEventSpool.recover(path, self.wave_id)
                recovered = _counter(recovery.iter_messages())
                recovery_facts = recovery.facts()
                if (
                    recovery_facts.get("incomplete") is not False
                    or recovery_facts.get("seal_marker_present") is not True
                    or recovery_facts.get("errors") != []
                ):
                    problems.append("server spool recovery incomplete")
            except Exception as exc:  # pylint: disable=broad-exception-caught
                problems.append("server spool recovery_" + type(exc).__name__)
        else:
            problems.append("server spool missing")
        delivered = _counter(self.messages)
        if recovered != delivered:
            problems.append("server spool/delivery mismatch")
        control = self.observer_evidence
        if not isinstance(control, dict):
            problems.append("server spool control unavailable")
            control = {}
        if (
            control.get("mode")
            != "commit-framed mmap during HTTP; seal and IPC publication after handlers and sampler stop"
        ):
            problems.append("server spool mode mismatch")
        spool = control.get("spool")
        if not isinstance(spool, dict):
            spool = {}
        expected = {
            "format": "g15-wave-event-spool-v1",
            "wave_id": self.wave_id,
            "path": path.name,
            "sealed": True,
            "closed": True,
            "fsync_succeeded": True,
            "incomplete": False,
            "errors": [],
            "error_count": 0,
            "emergency": None,
        }
        if any(
            type(spool.get(key)) is not type(value) or spool.get(key) != value
            for key, value in expected.items()
        ):
            problems.append("server spool seal/fsync/control invalid")
        if recovery_facts is not None:
            for key in (
                "sha256",
                "bytes",
                "used_bytes",
                "capacity_bytes",
                "committed_frames",
            ):
                if type(spool.get(key)) is not type(
                    recovery_facts.get(key)
                ) or spool.get(key) != recovery_facts.get(key):
                    problems.append("server spool control/recovery mismatch: " + key)
            count = recovery_facts["recovered_messages"]
            if (
                type(control.get("published_frames")) is not int
                or control["published_frames"] != count
                or type(spool.get("attempts")) is not int
                or spool["attempts"] != count
                or count != len(self.messages)
            ):
                problems.append("server spool publication count mismatch")
        publication = control.get("publication_resources")
        cost, resource_problems = _publication_cost(
            publication, self.server_process_id, self.rows
        )
        problems.extend(resource_problems)
        for name, kind in (
            (path.name, "server_spool"),
            (f"{self.wave_id}.events.emergency.ndjson", "server_emergency"),
        ):
            actual = self.directory / name
            if actual.exists():
                auxiliary.append(_file_receipt(actual, self.directory.parent, kind))
        if path.exists() and recovery_facts is not None:
            receipt = next(row for row in auxiliary if row["kind"] == "server_spool")
            if any(
                receipt[key] != recovery_facts.get(key) for key in ("sha256", "bytes")
            ):
                problems.append("server spool changed during recovery")
        if problems or self.errors:
            # Failure-only second copy: preserve every actually received envelope,
            # even if corrupt/cross-wave durable evidence cannot recover it.
            mismatch = self.directory / "server-delivery-mismatch.ndjson"
            with mismatch.open("xb") as handle:
                for message in self.messages:
                    handle.write(_json_bytes(message))
                handle.flush()
                os.fsync(handle.fileno())
            auxiliary.append(
                _file_receipt(
                    mismatch, self.directory.parent, "server_delivery_mismatch"
                )
            )
        self.errors.extend(problems)
        return {
            "recovery": recovery_facts,
            "control": self.observer_evidence,
            "checks": problems,
            "publication_cost": cost,
            "received_frames": len(self.messages),
            "recovered_frames": sum(recovered.values()),
        }

    def close(self) -> dict[str, Any]:
        if self._receipt is not None:
            return self._receipt
        with self.lock:
            if not self.closed:
                try:
                    self.handle.flush()
                    os.fsync(self.handle.fileno())
                except OSError as exc:
                    self.errors.append("raw_close_" + type(exc).__name__)
                finally:
                    self.handle.close()
                    self.closed = True
        auxiliary = []
        server = self._recover_server(auxiliary) if self.server_spooled else None
        fallback = self.directory / "completion-journal-failure.json"
        if fallback.exists():
            auxiliary.append(
                _file_receipt(fallback, self.directory.parent, "client_journal_failure")
            )
        raw = _file_receipt(
            self.path,
            self.directory.parent,
            "client_raw" if self.server_spooled else "legacy_raw",
        )
        raw.pop("kind")
        self._receipt = {
            **raw,
            "auxiliary": auxiliary,
            "total_bytes": raw["bytes"] + sum(row["bytes"] for row in auxiliary),
            "counts": dict(self.counts),
            "errors": list(self.errors),
            "server_spool": server,
            "count_basis": "successfully written complete records; failed/partial bytes remain in the actual hashed files",
            "one_copy_basis": (
                "one client response base64 copy in raw; original server frames in separately hashed durable spool; failure-only delivery/journal copies explicitly receipted"
                if self.server_spooled
                else "response bytes occur only in client records; no duplicated decoded bodies"
            ),
        }
        return self._receipt


def _post_shutdown_artifact_manifest(output: Path, indexed_bytes: int) -> dict:
    """Audit only after child shutdown; never rewrite the original index/receipts."""
    expected, wave_ids, problems = {}, set(), []
    index_path = output / "index.ndjson"
    if index_path.exists():
        with index_path.open("rb") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    wave = row["wave_id"]
                    wave_ids.add(wave)
                    for receipt in (
                        row["raw"],
                        row["report"],
                        *row["raw"].get("auxiliary", []),
                    ):
                        path = Path(receipt["path"])
                        if (
                            path.is_absolute()
                            or ".." in path.parts
                            or path.as_posix() in expected
                        ):
                            raise ValueError("invalid or repeated indexed path")
                        expected[path.as_posix()] = (receipt, wave)
                except (ValueError, KeyError, TypeError):
                    problems.append("post-shutdown index record invalid")
    else:
        problems.append("post-shutdown index unavailable")
    known = {name: wave for name, (_receipt, wave) in expected.items()}
    # Include an interrupted, not-yet-indexed wave only when its known files exist.
    for directory in output.iterdir():
        if directory.is_dir() and (
            directory.name in wave_ids
            or (directory / f"{directory.name}.events.spool").exists()
        ):
            wave_ids.add(directory.name)
    for wave in wave_ids:
        for name in (
            "raw.ndjson",
            "report.json",
            "server-delivery-mismatch.ndjson",
            "completion-journal-failure.json",
            f"{wave}.events.spool",
            f"{wave}.events.emergency.ndjson",
        ):
            path = output / wave / name
            if path.exists():
                known[path.relative_to(output).as_posix()] = wave
    for name in ("unattributed.events.spool", "unattributed.events.emergency.ndjson"):
        if (output / name).exists():
            known[name] = "unattributed"
    artifacts, affected, delta, wave_bytes = [], set(), 0, Counter()
    for name, wave in sorted(known.items()):
        path = output / name
        prior = expected.get(name, (None, None))[0]
        actual = None
        try:
            if path.exists():
                actual = _file_receipt(path, output, "post_shutdown_artifact")
        except OSError as exc:
            problems.append("post-shutdown artifact read_" + type(exc).__name__)
            affected.add(wave)
        status = (
            "missing" if actual is None else "new" if prior is None else "unchanged"
        )
        if (
            actual is not None
            and prior is not None
            and any(actual[key] != prior.get(key) for key in ("sha256", "bytes"))
        ):
            status = "changed"
        if status != "unchanged":
            affected.add(wave)
        delta += (actual["bytes"] if actual else 0) - (prior["bytes"] if prior else 0)
        if actual is not None and path.name != "report.json":
            wave_bytes[wave] += actual["bytes"]
        item = {"wave_id": wave, "status": status, "indexed": prior, "actual": actual}
        if (
            path.name.endswith(".events.spool")
            and status != "unchanged"
            and actual is not None
        ):
            try:
                recovery = WaveEventSpool.recover(path, wave)
                item["recovery"] = recovery.facts()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                item["recovery_error"] = type(exc).__name__
        artifacts.append(item)
    if affected:
        problems.append("post-shutdown observer artifacts new/changed/missing")
    over_budget = sorted(
        wave for wave, size in wave_bytes.items() if size > MAX_WAVE_BYTES
    )
    if over_budget:
        problems.append("post-shutdown wave byte budget exceeded")
    manifest = {
        "format": "g15-observation-post-shutdown-artifacts-v1",
        "scope": "after child shutdown; known raw/report/spool/emergency/delivery/journal artifacts and root unattributed spool; original index and files unchanged",
        "artifacts": artifacts,
        "affected_waves": sorted(affected),
        "problems": problems,
        "artifact_delta_bytes": delta,
        "indexed_stored_bytes": indexed_bytes,
        "actual_stored_bytes_before_manifest": indexed_bytes + delta,
        "wave_raw_and_auxiliary_bytes": dict(wave_bytes),
        "over_budget_waves": over_budget,
    }
    path = output / "post-shutdown-artifacts.json"
    # Exclusive final sidecar: an earlier failure or audit is never overwritten.
    with path.open("xb") as handle:
        handle.write(_json_bytes(manifest))
        handle.flush()
        os.fsync(handle.fileno())
    return {
        **_file_receipt(path, output, "post_shutdown_manifest"),
        "artifact_delta_bytes": delta,
        "affected_waves": sorted(affected),
        "problems": problems,
        "over_budget_waves": over_budget,
        "actual_stored_bytes": indexed_bytes + delta + path.stat().st_size,
    }


def iter_wave_records(output: Path, receipt: dict):
    """Read logical originals across v2 NDJSON and v3 client/spool media.

    Failure-only copies are receipted separately, never merged as extra requests.
    The caller must inspect quality/errors before interpreting recovered prefixes.
    """
    with (output / receipt["path"]).open("rb") as handle:
        for line in handle:
            yield json.loads(line)
    for item in receipt.get("auxiliary", []):
        if item["kind"] == "server_spool":
            recovery = WaveEventSpool.recover(
                output / item["path"], Path(item["path"]).parent.name
            )
            for message in recovery.iter_messages():
                yield {
                    "kind": message["kind"],
                    "wave_id": message["wave_id"],
                    "origin_wave_id": message["wave_id"],
                    "value": message["event"],
                }


class PersistedAccounting(RunAccounting):
    def __init__(self, evidence: WaveEvidence) -> None:
        super().__init__()
        self.evidence_writer = evidence

    def client_complete(self, record: ClientRecord) -> None:
        super().client_complete(record)
        self.evidence_writer.write("client", asdict(record))


class ObservationClient:
    """Real short-connection HTTP client with metered socket/request linkage."""

    def __init__(
        self,
        base_url: str,
        token: str,
        actor: str,
        accounting: RunAccounting,
        meter: SocketNetworkMeter,
        evidence: WaveEvidence,
    ) -> None:
        self.location = urlsplit(base_url)
        self.token = token
        self.actor = actor
        self.accounting = accounting
        self.meter = meter
        self.evidence_writer = evidence
        self.confirm_preparation = None

    def query(
        self,
        run_id: str,
        device: str,
        *,
        page_size: int = 50,
        windows_cursor: str | None = "",
        alarms_cursor: str | None = "",
        streams: str | None = None,
        launch_barrier: threading.Barrier | None = None,
        on_connected=None,
    ) -> tuple[int, dict[str, object]]:
        connection = MeteredHTTPConnection(
            self.location.hostname,
            self.location.port,
            timeout=5,
            network_meter=self.meter,
        )
        parameters = [f"page_size={page_size}"]
        if streams is not None:
            parameters.append(f"streams={quote(streams)}")
        if windows_cursor:
            parameters.append(f"windows_cursor={quote(windows_cursor)}")
        if alarms_cursor:
            parameters.append(f"alarms_cursor={quote(alarms_cursor)}")
        target = f"/v1/runs/{run_id}/devices/{device}/health?" + "&".join(parameters)
        preparation = {
            "actor_ref": self.actor,
            "connect_started_ns": time.perf_counter_ns(),
            "connected_ns": None,
            "barrier_released_ns": None,
            "request_sent": False,
        }
        started = self.accounting.client_start() if launch_barrier is None else None
        waiting, status, raw, body, error = None, 0, b"", {}, ""
        connection_ref = None
        try:
            connection.connect()
            local, peer = connection.sock.getsockname(), connection.sock.getpeername()
            connection_ref = {
                "local": {"host": str(local[0]), "port": int(local[1])},
                "peer": {"host": str(peer[0]), "port": int(peer[1])},
            }
            preparation["connected_ns"] = time.perf_counter_ns()
            # put* only builds the in-memory header buffer. endheaders below is
            # the first HTTP write; no response is delayed or held.
            connection.putrequest("GET", target)
            connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader("Connection", "close")
            preparation["headers_prepared_ns"] = time.perf_counter_ns()
            if on_connected is not None:
                on_connected(connection_ref)
            if launch_barrier is not None:
                launch_barrier.wait(timeout=10)
                preparation["barrier_released_ns"] = time.perf_counter_ns()
                started = self.accounting.client_start()
            connection.endheaders()
            preparation["request_sent"] = True
            waiting = time.perf_counter()
            response = connection.getresponse()
            status = response.status
            raw = response.read()
            decoded = json.loads(
                raw.decode("utf-8"),
                parse_float=_finite_float,
                parse_constant=_invalid_constant,
            )
            if isinstance(decoded, dict):
                body = decoded
            else:
                error = "non_object_json"
            if not error and status != 200:
                error = f"http_status_{status}"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            error = type(exc).__name__
            if hasattr(exc, "partial") and isinstance(exc.partial, bytes):
                raw = exc.partial
            if launch_barrier is not None and started is None:
                launch_barrier.abort()
        finally:
            if started is None:
                started = self.accounting.client_start()
            if waiting is None:
                waiting = time.perf_counter()
            completed = time.perf_counter()
            record = ClientRecord(
                user_id=self.actor,
                run_id=run_id,
                device_id=device,
                started_monotonic=started,
                waiting_monotonic=waiting,
                completed_monotonic=completed,
                status=status,
                server_request_id=str(body.get("request_id", "")),
                trace_id=str(body.get("trace_id", "")),
                error=error,
                response_bytes_base64=base64.b64encode(raw).decode("ascii"),
            )
            try:
                self.accounting.client_complete(record)
            finally:
                connection.close()
            mapping = {
                "request_id": record.server_request_id,
                "connection_ref": connection_ref,
                "request_context": {
                    "streams": streams,
                    "page_size": page_size,
                    "first_page_streams": [
                        name
                        for name, cursor in (
                            ("windows", windows_cursor),
                            ("alarms", alarms_cursor),
                        )
                        if not cursor and streams in (None, "both", name)
                    ],
                },
                "preparation": preparation,
                "latency_basis": (
                    "send to complete read and decode; TCP/barrier excluded"
                    if launch_barrier is not None
                    else "call start to complete read and decode; TCP included"
                ),
            }
            self.evidence_writer.write("request_connection", mapping)
            self.evidence_writer.rows["request_connection"].append(mapping)
        return status, body


def _concurrent_wave(clients: list[ObservationClient]) -> list:
    from .dispatcher import RequestSpec, dispatch_wave

    if not clients:
        raise ValueError("a concurrent wave needs clients")
    state = clients[0].evidence_writer
    journal = CompletionJournal(state, len(clients), defer_writes_until_finish=True)
    original_error = None
    try:
        specifications = [
            RequestSpec(
                base_url=client.location.geturl(),
                actor=client.actor,
                token=client.token,
                run_id=device_run_id(index % D0_DEVICE_COUNT),
                device_id=device_id(index % D0_DEVICE_COUNT),
                page_size=100,
            )
            for index, client in enumerate(clients)
        ]
        return dispatch_wave(
            specifications,
            accounting=clients[0].accounting,
            meter=clients[0].meter,
            confirm_preparation=clients[0].confirm_preparation,
            emit_completed=journal.emit,
        )
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        try:
            journal.finish()
        except BaseException:
            if original_error is None:
                raise
        finally:
            state.report["completion_journal"] = journal.facts()


def _counter(rows: list) -> Counter:
    return Counter(json.dumps(row, sort_keys=True, allow_nan=False) for row in rows)


def _source_equal(first: dict, last: dict) -> bool:
    return first.get("git_sha") == last.get("git_sha") and first.get(
        "content_sha256"
    ) == last.get("content_sha256")


def _connection_key(value: dict) -> tuple:
    local, peer = value.get("local") or {}, value.get("peer") or {}
    return local.get("host"), local.get("port"), peer.get("host"), peer.get("port")


def _request_network_checks(
    accounting: RunAccounting,
    transport: list,
    client: dict,
    server: dict,
    request_links: list,
) -> list[str]:
    problems = validate_network_pair(client, server)
    client_keys = {_connection_key(row) for row in client.get("connections", [])}
    server_keys = {_connection_key(row) for row in server.get("connections", [])}
    ids = {row.server_request_id for row in accounting.client_records}
    received = [row for row in transport if row.get("stage") == "server_received"]
    if len(received) != len(ids) or {row.get("request_id") for row in received} != ids:
        problems.append("request/socket server-received identity mismatch")
    linked = [_connection_key(row.get("connection_ref") or {}) for row in received]
    if len(set(linked)) != len(linked) or set(linked) != server_keys:
        problems.append("server request/socket connection mapping mismatch")
    if len(client_keys) != len(accounting.client_records):
        problems.append("client request/socket multiplicity mismatch")
    links = {
        row.get("request_id"): _connection_key(row.get("connection_ref") or {})
        for row in request_links
    }
    if (
        len(request_links) != len(ids)
        or set(links) != ids
        or set(links.values()) != client_keys
    ):
        problems.append("client request/socket connection mapping mismatch")
    for row in received:
        client_key = links.get(row.get("request_id"))
        server_key = _connection_key(row.get("connection_ref") or {})
        if client_key != (server_key[2], server_key[3], server_key[0], server_key[1]):
            problems.append("request client/server socket tuple mismatch")
    return problems


def _summary_resources(value: dict | None) -> dict | None:
    if value is None:
        return None
    return {key: item for key, item in value.items() if key != "samples"}


def _summary_accounting(value: dict) -> dict:
    return {
        key: item
        for key, item in value.items()
        if key not in ("client_records", "server_records")
    }


def _peak_target(accounting: dict, concurrent: int) -> bool:
    return accounting.get("trusted") is True and all(
        accounting.get("peaks", {}).get(side, {}).get("in_flight", 0) >= concurrent
        and (
            accounting.get("peaks", {}).get(side, {}).get("distinct_users_at_peak") or 0
        )
        >= concurrent
        for side in ("client", "server")
    )


class ObservationSession:
    """One HTTP process and one drained stream, with explicit wave boundaries."""

    def __init__(self) -> None:
        self.process = None
        self.control = None
        self.receiver = None
        self.reader = None
        self.current: WaveEvidence | None = None
        self.stream_errors: list[str] = []
        self.ready: dict = {}
        self.base_url = ""

    def start(
        self,
        runs_root: Path,
        bearer: bytes,
        cursor: bytes,
        users: int,
        workers: int,
        evidence_root: Path | None = None,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        self.control, child = context.Pipe()
        self.receiver, sender = context.Pipe(duplex=False)
        self.process = context.Process(
            target=serve_endpoint,
            args=(
                child,
                sender,
                str(runs_root),
                bearer,
                cursor,
                users,
                workers,
                str(evidence_root) if evidence_root is not None else None,
            ),
            name="g15-observation-server",
        )
        self.process.start()
        child.close()
        sender.close()
        self.reader = threading.Thread(target=self._read_stream, daemon=True)
        self.reader.start()
        self.ready = _receive(self.control, "ready")
        self.base_url = self.ready["base_url"]

    def _read_stream(self) -> None:
        try:
            while True:
                message = self.receiver.recv()
                state = self.current
                if state is None:
                    self.stream_errors.append("event outside an active wave")
                else:
                    state.stream(message)
        except EOFError:
            pass
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.stream_errors.append(type(exc).__name__)

    def close(self) -> list[str]:
        errors = []
        if self.process is not None:
            if self.process.is_alive():
                try:
                    self.control.send({"kind": "stop"})
                except (OSError, EOFError):
                    pass
                self.process.join(timeout=10)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=5)
                    errors.append("server stop timeout")
            if self.process.exitcode != 0:
                errors.append("server process exit failure")
        if self.reader is not None:
            self.reader.join(timeout=5)
            if self.reader.is_alive():
                errors.append("stream reader did not reach EOF")
        if self.control is not None:
            self.control.close()
        if self.receiver is not None:
            self.receiver.close()
        return errors + list(self.stream_errors)

    def _confirm_preparation(self, references):
        from .preparation import verify_preparation

        state = self.current
        if state is None:
            raise RuntimeError("no wave for connection preparation")
        record = {
            "server": None,
            "client_references": references,
            "client_wait_started_ns": time.perf_counter_ns(),
            "client_confirmed_ns": None,
            "client_wait_completed_ns": None,
            "error_type": None,
            "problems": [],
            "basis": "accepted TCP and handler reader installed, active as of server snapshot before HTTP send; no lifetime extension; not received/executing or an authenticated user",
        }
        failure = None
        try:
            self.control.send(
                {
                    "kind": "await_prepared",
                    "wave_id": state.wave_id,
                    "connections": references,
                }
            )
            ack = _receive(self.control, "prepared")
            record["server"] = ack
            record["problems"] = verify_preparation(
                ack,
                references,
                wave_id=state.wave_id,
                process_id=self.ready["process_id"],
            )
            if record["problems"]:
                raise RuntimeError("invalid connection preparation acknowledgement")
            record["client_confirmed_ns"] = time.perf_counter_ns()
        except BaseException as exc:
            failure = exc
            record["error_type"] = type(exc).__name__
            details = getattr(exc, "details", None)
            if isinstance(details, dict):
                # The child's error is an exception type, never exception text.
                name = details.get("error")
                if isinstance(name, str) and name.isidentifier() and len(name) <= 64:
                    record["server_control_error_type"] = name
            raise
        finally:
            record["client_wait_completed_ns"] = time.perf_counter_ns()
            state.report["connection_preparation"] = record
            try:
                state.write("preparation", record)
            except BaseException:
                if failure is None:
                    raise

    def observe(
        self,
        output: Path,
        wave_id: str,
        identities: list,
        db_paths: list[Path],
        *,
        prelude: bool = False,
    ) -> tuple[dict, dict]:
        """Retain interrupted raw evidence before propagating the original signal."""
        try:
            return self._observe(output, wave_id, identities, db_paths, prelude=prelude)
        except BaseException as exc:
            state = self.current
            if state is not None:
                report = state.report or {
                    "wave_id": wave_id,
                    "phase": "serial10_and_full_paging" if prelude else "concurrent",
                    "checks": {},
                    "failures": [],
                }
                _failure(
                    report,
                    (
                        "observation_interrupted"
                        if not isinstance(exc, Exception)
                        else "observation_unhandled"
                    ),
                    exc,
                )
                report.update(
                    interrupted=not isinstance(exc, Exception),
                    quality_passed=False,
                    target_reached=False,
                    gate0_passed=False,
                    exit_code=1,
                )
                report.setdefault("cleanup_errors", [])
                if not state.closed and not state.end.is_set():
                    if state.client_sampler is not None:
                        try:
                            report.setdefault("endpoint_resources", {})[
                                "client"
                            ] = state.client_sampler.stop()
                        except BaseException as cleanup:
                            report["cleanup_errors"].append(type(cleanup).__name__)
                    try:
                        self.control.send(
                            {
                                "kind": "finish_wave",
                                "expected_completed": (
                                    len(state.accounting.client_records)
                                    if state.accounting is not None
                                    else 0
                                ),
                            }
                        )
                        finished = _receive(self.control, "finished")
                        state.observer_evidence = finished.get("observer_evidence")
                        if not state.end.wait(20):
                            report["cleanup_errors"].append("end_marker_missing")
                        report.setdefault("endpoint_resources", {})["server"] = (
                            finished.get("resources")
                        )
                    except BaseException as cleanup:
                        report["cleanup_errors"].append(type(cleanup).__name__)
                report["cleanup_errors"].extend(self.close())
                report.pop("transport_events", None)
                report["endpoint_resources"] = {
                    side: _summary_resources(value)
                    for side, value in report.get("endpoint_resources", {}).items()
                }
                report["raw_evidence"] = state.close()
                if state.server_spooled:
                    report["observer_evidence"] = report["raw_evidence"]["server_spool"]
                    report.setdefault("checks", {})["observer_evidence"] = report[
                        "observer_evidence"
                    ]["checks"]
                report["completed_requests"] = (
                    len(state.accounting.client_records)
                    if state.accounting is not None
                    else 0
                )
                report_path = state.directory / "report.json"
                _atomic_json(report_path, report)
                facts = {
                    "wave_id": wave_id,
                    "phase": report["phase"],
                    "report": {
                        "path": f"{wave_id}/report.json",
                        "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                        "bytes": report_path.stat().st_size,
                    },
                    "raw": report["raw_evidence"],
                    "quality_passed": False,
                    "target_reached": False,
                    "interrupted": report["interrupted"],
                }
                self.current = None
                exc.observation_wave_result = (report, facts)
            raise

    def _observe(
        self,
        output: Path,
        wave_id: str,
        identities: list,
        db_paths: list[Path],
        *,
        prelude: bool = False,
    ) -> tuple[dict, dict]:
        state = WaveEvidence(
            output,
            wave_id,
            server_spooled=self.ready.get("observer_event_mode")
            == "commit-framed-spool",
        )
        state.server_process_id = self.ready["process_id"]
        self.current = state
        accounting = PersistedAccounting(state) if prelude else RunAccounting()
        meter = SocketNetworkMeter("client")
        clients = [
            ObservationClient(self.base_url, token, actor, accounting, meter, state)
            for token, actor in identities
        ]
        report: dict[str, Any] = {
            "format": "g15-d0-observation-wave-v3",
            "wave_id": wave_id,
            "phase": "serial10_and_full_paging" if prelude else "concurrent",
            "client_process_id": os.getpid(),
            "server_ready_process_id": self.ready["process_id"],
            "source_ref": "source.json",
            "checks": {},
            "failures": [],
            "quality_passed": False,
            "target_reached": False,
            "gate0_passed": False,
            "endpoint_resources": {"client": None, "server": None},
            "latency_basis": (
                "prelude calls include TCP setup; complete raw paging preserved"
                if prelude
                else "each concurrent request starts at its first nonblocking socket send attempt after TCP/header preparation and exact ACK; terminal is immediate full read and decode; journal drain excluded"
            ),
        }
        state.report = report
        state.accounting = accounting
        responses, walk = [], PagingWalk()
        sampler = EndpointSampler(
            "client", on_sample=lambda row: state.write("resource_client", row)
        )
        state.client_sampler = sampler
        finished = None
        try:
            self.control.send({"kind": "begin_wave", "wave_id": wave_id})
            begun = _receive(self.control, "wave_started")
            if begun.get("wave_id") != wave_id:
                raise RuntimeError("begin wave identity mismatch")
            sampler.start()
            if prelude:
                for index in range(10):
                    run_id = device_run_id(index)
                    status, body = clients[0].query(
                        run_id, device_id(index), page_size=100
                    )
                    responses.append((status, body, run_id))
                walk = run_paging(clients[0], 0, db_paths)
            else:
                for client in clients:
                    client.confirm_preparation = self._confirm_preparation
                responses = _concurrent_wave(clients)
                if responses:
                    first = responses[0]
                    walk = PagingWalk(
                        raw=[first],
                        consumed=[first],
                        request_contexts=[
                            {
                                "request_id": first[1].get("request_id"),
                                "streams": None,
                                "page_size": 100,
                                "first_page_streams": ["windows", "alarms"],
                            }
                        ],
                        discards=[
                            {
                                "page": 0,
                                "request_id": first[1].get("request_id"),
                                "windows": 0,
                                "alarms": 0,
                                "reason": "complete actual page100; no discard",
                            }
                        ],
                    )
                    if first[0] == 200:
                        db_path = next(
                            path
                            for path in db_paths
                            if path.parent.parent.name == first[2]
                        )
                        walk.oracle_problems.extend(
                            verify_response(
                                first[1],
                                db_path,
                                first[2],
                                first[1].get("device_id"),
                                page_size=100,
                                first_page=True,
                            )
                        )
                    else:
                        walk.oracle_problems.append(
                            "coverage response status is not 200"
                        )
                    responses = responses[1:]
            report["endpoint_resources"]["client"] = sampler.stop()
            self.control.send(
                {
                    "kind": "finish_wave",
                    "expected_completed": len(accounting.client_records),
                }
            )
            finished = _receive(self.control, "finished")
            state.observer_evidence = finished.get("observer_evidence")
            if finished.get("wave_id") != wave_id or not state.end.wait(20):
                raise RuntimeError("end wave marker missing or mismatched")
            report["endpoint_resources"]["server"] = finished["resources"]
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _failure(report, "observation_wave", exc)
            if report["endpoint_resources"]["client"] is None:
                report["endpoint_resources"]["client"] = sampler.stop()
            if finished is None:
                try:
                    self.control.send(
                        {
                            "kind": "finish_wave",
                            "expected_completed": len(accounting.client_records),
                        }
                    )
                    finished = _receive(self.control, "finished")
                    state.observer_evidence = finished.get("observer_evidence")
                    state.end.wait(20)
                    report["endpoint_resources"]["server"] = finished.get("resources")
                except (
                    Exception
                ) as snapshot_error:  # pylint: disable=broad-exception-caught
                    _failure(report, "observation_wave_snapshot", snapshot_error)
                    report["shutdown_errors"] = self.close()
        try:
            for event in state.rows["transport"]:
                accounting.server_event(event)
            report["transport_events"] = state.rows["transport"]
            report["checks"]["stream"] = (
                list(state.errors)
                + list(self.stream_errors)
                + (finished or {}).get("stream_errors", [])
            )
            if finished is None:
                report["checks"]["stream"].append("finished snapshot unavailable")
            else:
                for kind, key in (
                    ("transport", "transport_events"),
                    ("application", "application_events"),
                ):
                    if _counter(state.rows[kind]) != _counter(finished[key]):
                        report["checks"]["stream"].append(
                            f"{kind} stream/snapshot mismatch"
                        )
                if _counter(state.rows["resource"]) != _counter(
                    (finished.get("resources") or {}).get("samples", [])
                ):
                    report["checks"]["stream"].append(
                        "resource stream/snapshot mismatch"
                    )
                if finished.get("active_handlers") != 0 or finished.get(
                    "drain_problems"
                ):
                    report["checks"]["stream"].append(
                        "server handler/completion drain failed"
                    )
            validation = validate_run(
                responses,
                walk,
                db_paths,
                accounting,
                state.rows["application"],
                page_size=100,
                paging_page_size=10 if prelude else 100,
            )
            report["checks"].update(validation["checks"])
            report["checks"]["resources"] = _validate_resources(report)
            client_network = meter.snapshot()
            control_network = (finished or {}).get("network") or {}
            if len(state.rows["network"]) != 1 or _counter(
                state.rows["network"]
            ) != _counter([control_network]):
                report["checks"]["stream"].append("network stream/snapshot mismatch")
            server_network = (
                state.rows["network"][0] if len(state.rows["network"]) == 1 else {}
            )
            state.write("network_client", client_network)
            report["checks"]["network"] = _request_network_checks(
                accounting,
                state.rows["transport"],
                client_network,
                server_network,
                state.rows["request_connection"],
            )
            markers = state.rows["marker"]
            marker_valid = (
                [row.get("marker") for row in markers] == ["begin", "end"]
                and all(
                    row.get("wave_id") == wave_id
                    and row.get("process_id") == self.ready["process_id"]
                    and type(row.get("monotonic_ns")) is int
                    and row["monotonic_ns"] > 0
                    for row in markers
                )
                and markers[0]["monotonic_ns"] < markers[1]["monotonic_ns"]
            )
            report["checks"]["markers"] = (
                [] if marker_valid else ["wave marker identity/clock sequence mismatch"]
            )
            report["accounting"] = _summary_accounting(accounting.evidence())
            report["target_reached"] = _peak_target(
                report["accounting"], 1 if prelude else len(identities)
            )
            report["completed_requests"] = len(accounting.client_records)
            report["full_paging"] = {
                "kind": (
                    "actual page10 walk"
                    if prelude
                    else "single complete actual page100 coverage view"
                ),
                "raw_pages": len(walk.raw),
                "consumed_pages": len(walk.consumed),
                "discard_counts": walk.evidence().get("counts"),
                "request_contexts": walk.request_contexts,
            }
            records = accounting.client_records
            report["load_interval"] = {
                "start_monotonic": min(
                    (r.started_monotonic for r in records), default=None
                ),
                "end_monotonic": max(
                    (r.completed_monotonic for r in records), default=None
                ),
                "seconds": (
                    max(r.completed_monotonic for r in records)
                    - min(r.started_monotonic for r in records)
                    if records
                    else 0.0
                ),
                "clock": "client perf_counter only; no cross-process subtraction",
                "counted_toward_duration": not prelude and bool(records),
            }
            if not records:
                _failure(report, "no_http_started", RuntimeError())
            report["endpoint_resources"] = {
                side: _summary_resources(value)
                for side, value in report["endpoint_resources"].items()
            }
            report["network"] = {
                side: {
                    key: value
                    for key, value in snapshot.items()
                    if key != "connections"
                }
                for side, snapshot in (
                    ("client", client_network),
                    ("server", server_network),
                )
            }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _failure(report, "observation_diagnostics", exc)
            report["target_reached"] = False
        # Raw records already persisted. Ordinary report carries only summaries.
        report.pop("transport_events", None)
        report["endpoint_resources"] = {
            side: _summary_resources(value)
            for side, value in report["endpoint_resources"].items()
        }
        report["raw_evidence"] = state.close()
        if state.server_spooled:
            report["observer_evidence"] = report["raw_evidence"]["server_spool"]
            report["checks"]["observer_evidence"] = report["observer_evidence"][
                "checks"
            ]
        report["quality_passed"] = (
            not report.get("failures")
            and bool(report["checks"])
            and not any(report["checks"].values())
            and report["raw_evidence"]["total_bytes"] <= MAX_WAVE_BYTES
            and not report["raw_evidence"]["errors"]
        )
        if report["raw_evidence"]["total_bytes"] > MAX_WAVE_BYTES:
            _failure(report, "wave_byte_budget", RuntimeError())
        report["exit_code"] = int(
            not (report["quality_passed"] and report["target_reached"])
        )
        path = state.directory / "report.json"
        _atomic_json(path, report)
        facts = {
            "wave_id": wave_id,
            "phase": report["phase"],
            "report": {
                "path": f"{wave_id}/report.json",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            },
            "raw": report["raw_evidence"],
            "quality_passed": report["quality_passed"],
            "target_reached": report["target_reached"],
            "load_interval": report.get("load_interval"),
            "peaks": report.get("accounting", {}).get("peaks"),
            "server_executing": report.get("accounting", {}).get("server_executing"),
            "latency_seconds": report.get("accounting", {}).get("latency_seconds"),
        }
        self.current = None
        return report, facts


def run_observation(
    output: Path,
    *,
    concurrent: int = 10,
    duration_seconds: float = 60,
    workers: int = 64,
    max_waves: int = DEFAULT_MAX_WAVES,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
    min_waves: int = 1,
) -> dict[str, Any]:
    """Observe actual load intervals, retaining every wave and every target miss.

    Short durations are for automated diagnostic tests only. The CLI enforces
    at least 60 seconds and a short test never earns the one-minute flag.
    """
    if (
        concurrent not in (10, 50)
        or not concurrent <= workers <= 64
        or not 0 < duration_seconds <= 3600
        or not 1 <= min_waves <= max_waves <= DEFAULT_MAX_WAVES
        or not MAX_WAVE_BYTES <= byte_budget <= DEFAULT_BYTE_BUDGET
    ):
        raise ValueError("invalid D0 observation duration, load or evidence budget")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    summary: dict[str, Any] = {
        "format": "g15-d0-observation-v3",
        "requested_concurrent_users": concurrent,
        "requested_load_seconds": duration_seconds,
        "formal_duration_requirement_seconds": 60,
        "observed_load_seconds": 0.0,
        "attempted_load_seconds": 0.0,
        "wall_observation_seconds": 0.0,
        "between_wave_gap_seconds": 0.0,
        "completed_waves": 0,
        "target_reached_waves": 0,
        "all_waves_target_reached": True,
        "all_waves_quality_passed": True,
        "quality_passed": False,
        "duration_met": False,
        "formal_one_minute_met": False,
        "gate0_passed": False,
        "exit_code": 1,
        "failures": [],
        "active_wave": None,
        "workload": {
            "devices": 10,
            "windows_per_device": 100,
            "page_size": 100,
            "same_server_process_for_all_waves": True,
            "client_dispatcher": "selectors.DefaultSelector",
            "completion_evidence": "bounded frozen terminal pairs; writer starts after all HTTP dispatch ends; drain excluded from request latency and included in wall/gap costs",
            "serial_smoke": "one user / ten requests before observation",
            "paging": "complete actual page10 traversal before observation",
            "arrival": "selector dispatcher after real TCP/header and exact reader ACK; independent actual first-send timestamps; read-ready checked after each bounded write",
            "duration_basis": "sum of quality-verified actual concurrent send-to-read-and-decode spans; all gaps excluded; failed attempts retained separately",
            "cache": "uncontrolled; no cold-cache claim",
            "delays": "no sleep, response holding, server barrier, throttle or retry",
        },
        "bounds": {
            "max_waves": max_waves,
            "byte_budget": byte_budget,
            "wave_reservation_bytes": MAX_WAVE_BYTES,
            "in_memory": "one wave: up to 50 client records and corresponding transport/application events plus resource samples and network metadata; server file-backed mmap capacity <=16MiB and small JSON scratch <=64KiB; logical event counts do not bound all Python object memory",
            "raw_io": "one client base64 copy in per-wave raw.ndjson; original server metadata in commit-framed mmap spool, sealed/fsynced then IPC-published after HTTP; all auxiliary bytes count against budgets",
        },
        "observer_publication": {
            "scope": "measured server before/after: post-handler network snapshot, network/end append, seal/fsync and event IPC; excludes finished control/facts hash, parent validation and teardown; request latency/effective load seconds exclude all post-HTTP work",
            "measured_waves": 0,
            "unavailable_waves": [],
            "wall_seconds": 0.0,
            "cpu_seconds": 0.0,
        },
        "peak_ranges": {
            side: {"min": None, "max": None} for side in ("client", "server")
        },
        "remaining_gates": [
            "independent non-author review",
            "two Linux evidence on this exact source",
            "application queue absent; N/A scope requires real review and ruling",
            "selected-stream raw paging on final source requires independent review",
            "TCP/IP/link overhead, retransmissions and host network saturation unmeasured",
            "cache cold/hot evidence requires controlled environment",
        ],
    }
    _atomic_json(output / "summary.json", summary)
    index_path = output / "index.ndjson"
    index_digest, index_bytes, stored_bytes = hashlib.sha256(), 0, 0
    session = ObservationSession()
    source_before = None
    first_load_start, last_load_end = None, None
    pending_interruption = None

    def save_wave(report: dict, facts: dict, index_handle: Any) -> None:
        nonlocal stored_bytes, index_bytes
        payload = _json_bytes(facts)
        index_handle.write(payload)
        index_handle.flush()
        os.fsync(index_handle.fileno())
        index_digest.update(payload)
        index_bytes += len(payload)
        stored_bytes += (
            facts["raw"].get("total_bytes", facts["raw"]["bytes"])
            + facts["report"]["bytes"]
            + len(payload)
        )
        summary["stored_evidence_bytes"] = stored_bytes
        if stored_bytes > byte_budget:
            summary["stop_reason"] = "evidence_byte_budget_exceeded"
            _failure(summary, "evidence_byte_budget_exceeded", RuntimeError())
        cost = report.get("observer_evidence", {}).get("publication_cost", {})
        publication = summary["observer_publication"]
        if cost.get("valid") is True:
            publication["measured_waves"] += 1
            for key in ("wall_seconds", "cpu_seconds"):
                publication[key] += cost[key]
        else:
            publication["unavailable_waves"].append(facts["wave_id"])
        summary["active_wave"] = None
        _atomic_json(output / "summary.json", summary)

    def retained_observe(
        wave_id: str,
        identities: list,
        db_paths: list[Path],
        index_handle: Any,
        *,
        prelude: bool = False,
    ):
        try:
            return session.observe(
                output, wave_id, identities, db_paths, prelude=prelude
            )
        except BaseException as exc:
            retained = getattr(exc, "observation_wave_result", None)
            if retained is not None:
                report, facts = retained
                if prelude:
                    summary["prelude_quality_passed"] = False
                else:
                    summary["completed_waves"] += 1
                    summary["all_waves_quality_passed"] = False
                    summary["all_waves_target_reached"] = False
                save_wave(report, facts, index_handle)
            raise

    try:
        source_before = source_facts()
        _atomic_json(output / "source.json", source_before)
        summary["source"] = {
            "git_sha": source_before.get("git_sha"),
            "git_dirty": source_before.get("git_dirty"),
            "source_file_sha256": hashlib.sha256(
                (output / "source.json").read_bytes()
            ).hexdigest(),
        }
        with tempfile.TemporaryDirectory(prefix="g15-observation-d0-") as temporary:
            db_paths = build_d0_runs(Path(temporary) / "runs")
            _atomic_json(
                output / "query-plans.json", query_plans(db_paths, page_size=100)
            )
            summary["query_plans"] = {
                "path": "query-plans.json",
                "sha256": hashlib.sha256(
                    (output / "query-plans.json").read_bytes()
                ).hexdigest(),
                "scope": "actual application SELECT helper probe; not full HTTP trace or a cold-cache claim",
            }
            bearer, cursor = secrets.token_bytes(32), secrets.token_bytes(32)
            identities = _identities(bearer, concurrent)
            session.start(
                Path(temporary) / "runs",
                bearer,
                cursor,
                concurrent,
                workers,
                evidence_root=output,
            )
            summary["server_process_id"] = session.ready["process_id"]
            with index_path.open("xb") as index_handle:
                summary["active_wave"] = "prelude"
                _atomic_json(output / "summary.json", summary)
                prelude, facts = retained_observe(
                    "prelude", identities, db_paths, index_handle, prelude=True
                )
                summary["prelude_quality_passed"] = prelude["quality_passed"]
                save_wave(prelude, facts, index_handle)
                if stored_bytes > byte_budget:
                    raise RuntimeError("actual retained evidence exceeds byte budget")
                if not prelude["quality_passed"]:
                    summary["stop_reason"] = "prelude_quality_failed"
                    raise RuntimeError("prelude quality failed")
                while (
                    summary["observed_load_seconds"] < duration_seconds
                    or summary["completed_waves"] < min_waves
                ):
                    if summary["completed_waves"] >= max_waves:
                        summary["stop_reason"] = "wave_budget_exhausted"
                        raise RuntimeError("wave budget exhausted before duration")
                    if stored_bytes + MAX_WAVE_BYTES > byte_budget:
                        summary["stop_reason"] = "evidence_byte_budget_exhausted"
                        raise RuntimeError("byte budget exhausted before duration")
                    if shutil.disk_usage(output).free < MAX_WAVE_BYTES:
                        summary["stop_reason"] = "evidence_volume_free_space"
                        raise RuntimeError("insufficient evidence-volume free space")
                    wave_id = f"wave-{summary['completed_waves'] + 1:05d}"
                    summary["active_wave"] = wave_id
                    _atomic_json(output / "summary.json", summary)
                    report, facts = retained_observe(
                        wave_id, identities, db_paths, index_handle
                    )
                    summary["completed_waves"] += 1
                    summary["all_waves_quality_passed"] &= report["quality_passed"]
                    summary["all_waves_target_reached"] &= report["target_reached"]
                    summary["target_reached_waves"] += int(report["target_reached"])
                    interval = report.get("load_interval") or {}
                    summary["attempted_load_seconds"] += max(
                        0, interval.get("seconds", 0)
                    )
                    if report["quality_passed"] and interval.get("seconds", 0) > 0:
                        if first_load_start is None:
                            first_load_start = interval["start_monotonic"]
                        last_load_end = interval["end_monotonic"]
                        summary["observed_load_seconds"] += interval["seconds"]
                        summary["wall_observation_seconds"] = (
                            last_load_end - first_load_start
                        )
                        summary["between_wave_gap_seconds"] = (
                            summary["wall_observation_seconds"]
                            - summary["observed_load_seconds"]
                        )
                    for side in ("client", "server"):
                        peak = (facts.get("peaks") or {}).get(side, {}).get("in_flight")
                        if isinstance(peak, int):
                            bounds = summary["peak_ranges"][side]
                            bounds["min"] = (
                                peak
                                if bounds["min"] is None
                                else min(bounds["min"], peak)
                            )
                            bounds["max"] = (
                                peak
                                if bounds["max"] is None
                                else max(bounds["max"], peak)
                            )
                    save_wave(report, facts, index_handle)
                    if stored_bytes > byte_budget:
                        raise RuntimeError(
                            "actual retained evidence exceeds byte budget"
                        )
                    if not report["quality_passed"]:
                        summary["stop_reason"] = "wave_quality_failed"
                        raise RuntimeError("wave quality failed; observation stopped")
    except BaseException as exc:
        if not isinstance(exc, Exception):
            summary["interrupted"] = True
            summary["stop_reason"] = "observation_interrupted"
            pending_interruption = exc
        _failure(summary, summary.get("stop_reason", "observation"), exc)
    finally:
        summary["shutdown_errors"] = session.close()
        if summary["shutdown_errors"]:
            _failure(summary, "observation_shutdown", RuntimeError())
        try:
            manifest = _post_shutdown_artifact_manifest(output, stored_bytes)
            summary["post_shutdown_artifacts"] = manifest
            summary["stored_evidence_bytes"] = manifest["actual_stored_bytes"]
            if manifest["problems"]:
                _failure(summary, "post_shutdown_artifacts", RuntimeError())
            if manifest["actual_stored_bytes"] > byte_budget:
                _failure(summary, "evidence_byte_budget_exceeded", RuntimeError())
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _failure(summary, "post_shutdown_artifact_audit", exc)
        try:
            source_after = source_facts()
            summary["source_after"] = {
                "git_sha": source_after.get("git_sha"),
                "git_dirty": source_after.get("git_dirty"),
                "content_sha256": source_after.get("content_sha256"),
            }
            summary["source_unchanged"] = source_before is not None and _source_equal(
                source_before, source_after
            )
            if not summary["source_unchanged"]:
                _failure(summary, "fixed_source", RuntimeError())
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _failure(summary, "source_snapshot", exc)
        summary["index"] = {
            "path": "index.ndjson",
            "sha256": index_digest.hexdigest(),
            "bytes": index_bytes,
        }
        summary["duration_met"] = summary["observed_load_seconds"] >= duration_seconds
        summary["formal_one_minute_met"] = summary["observed_load_seconds"] >= 60
        summary["quality_passed"] = (
            not summary["failures"]
            and summary.get("prelude_quality_passed") is True
            and summary["all_waves_quality_passed"]
            and summary["completed_waves"] > 0
        )
        if summary["completed_waves"] == 0:
            summary["all_waves_target_reached"] = False
        summary["target_reached"] = (
            summary["quality_passed"]
            and summary["duration_met"]
            and summary["all_waves_target_reached"]
        )
        summary["exit_code"] = int(not summary["target_reached"])
        summary["gate0_passed"] = False
        _atomic_json(output / "summary.json", summary)
    if pending_interruption is not None:
        raise pending_interruption
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Opt-in D0 one-minute actual-load observation"
    )
    parser.add_argument("--concurrent", type=int, choices=(10, 50), required=True)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--max-waves", type=int, default=DEFAULT_MAX_WAVES)
    parser.add_argument("--byte-budget", type=int, default=DEFAULT_BYTE_BUDGET)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.seconds < 60:
        parser.error(
            "--seconds must be at least 60; short duration is diagnostic-test-only"
        )
    if args.output.exists():
        parser.error("--output exists; previous evidence must be retained")
    summary = run_observation(
        args.output,
        concurrent=args.concurrent,
        duration_seconds=args.seconds,
        workers=args.workers,
        max_waves=args.max_waves,
        byte_budget=args.byte_budget,
    )
    for key in (
        "completed_waves",
        "observed_load_seconds",
        "wall_observation_seconds",
        "between_wave_gap_seconds",
        "all_waves_target_reached",
        "quality_passed",
        "formal_one_minute_met",
        "gate0_passed",
        "exit_code",
    ):
        print(key, summary[key])
    return int(summary["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
