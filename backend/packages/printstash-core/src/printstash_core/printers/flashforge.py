"""Framework-neutral Flashforge HTTP provider client (Adventurer 5M family, AD5X).

The printer serves JSON on port 8898; every request is a POST carrying the
serial number and check code. It keeps no print history and its status moves
through transient states (``canceling`` also appears during a pause, a cancel
ends in ``ready``), so the outcome of a print is decided across polls by
:class:`FlashforgeJobTracker` rather than read from one response.

The client is read-only: it never sends ``/control`` commands, so it cannot
start, stop or reconfigure a print.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never
from urllib.parse import urlsplit

import httpx

from .catalog import PROVIDER_DEFINITIONS
from .contracts import PrinterClient, SnapshotCallback
from .models import (
    FlashforgeConfig,
    PrinterConfig,
    PrinterSnapshot,
    ProviderError,
    ProviderId,
)

DEFAULT_PORT = 8898
# The printer answers /detail in a few ms; a short interval is what lets the
# tracker see `completed` and `canceling`, which can last only seconds.
POLL_INTERVAL_S = 5.0

FLASHFORGE_CAPABILITIES = PROVIDER_DEFINITIONS[ProviderId.FLASHFORGE].capabilities

_RUNNING = frozenset({"printing", "heating", "pausing"})
_PAUSED = frozenset({"pause", "paused"})
# Seen mid-job without ending it: `canceling` also occurs during a pause
# sequence, and `error` can be recovered from on the printer's screen.
_HOLD = frozenset({"canceling", "error", "busy", "calibrate_doing", "downloading"})
# Progress at or above this counts as finished when `completed` was missed.
_DONE_PROGRESS = 0.99

HttpClientFactory = Callable[..., httpx.AsyncClient]


class FlashforgeError(RuntimeError):
    def __init__(self, detail: str, *, code: str = "provider_transport_error") -> None:
        super().__init__(detail)
        self.code = code


def base_url_for(host: str) -> str:
    """Accept ``192.168.1.10``, ``host:8898`` or ``http://host`` alike."""

    value = host.strip().rstrip("/")
    if "://" not in value:
        value = f"http://{value}"
    parts = urlsplit(value)
    if not parts.hostname:
        raise FlashforgeError(
            "flashforge_host_invalid", code="provider_credentials_missing"
        )
    return f"{parts.scheme}://{parts.hostname}:{parts.port or DEFAULT_PORT}"


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except TypeError, ValueError:
        return None


@dataclass
class _ActiveJob:
    filename: str
    progress: float = 0.0
    duration_s: float | None = None
    weight_g: float | None = None
    length_mm: float | None = None
    layer: int | None = None
    total_layers: int | None = None
    cancel_seen: bool = False
    error: str | None = None


class FlashforgeJobTracker:
    """Turn successive ``/detail`` payloads into one terminal state per print."""

    def __init__(self) -> None:
        self.job: _ActiveJob | None = None
        self.last_state = "standby"

    def observe(self, detail: Mapping[str, Any]) -> dict[str, Any]:
        raw = str(detail.get("status") or "").strip().lower()
        filename = str(detail.get("printFileName") or "").strip()
        job = self.job

        if raw in _RUNNING or raw in _PAUSED:
            if filename and (job is None or job.filename != filename):
                job = self.job = _ActiveJob(filename)
            if job is not None:
                self._record(job, detail)
                job.cancel_seen = False
                job.error = None
            return self._report("printing" if raw in _RUNNING else "paused", detail)

        if job is not None and raw in _HOLD:
            self._record(job, detail)
            if raw == "canceling":
                job.cancel_seen = True
            if raw == "error":
                job.error = f"printer_error {detail.get('errorCode') or ''}".strip()
            # A held job keeps its last state; `error` shows as paused so the
            # print stays open until the printer settles one way or the other.
            held = "paused" if raw == "error" else self.last_state
            return self._report(held, detail, message=job.error)

        if raw in {"completed", "cancel"}:
            state = "complete" if raw == "completed" else "cancelled"
            if job is None and filename:
                # Already over when first seen (PrintStash was down meanwhile);
                # the hub treats a repeat of a logged ending as a no-op.
                job = self.job = _ActiveJob(filename)
            if job is not None:
                self._record(job, detail)
            return self._finish(state, detail)

        if job is not None:
            # The printer is idle again without having said how the print ended.
            if job.cancel_seen:
                state = "cancelled"
            elif job.error:
                state = "error"
            elif job.progress >= _DONE_PROGRESS or (
                job.layer and job.total_layers and job.layer >= job.total_layers
            ):
                state = "complete"
            else:
                # Unknown ending: let the hub record the job as disappeared.
                state = "standby"
            return self._finish(state, detail)

        return self._report("standby", detail)

    @staticmethod
    def _record(job: _ActiveJob, detail: Mapping[str, Any]) -> None:
        # Idle payloads zero these fields, so keep the last values seen mid-job.
        progress = _number(detail.get("printProgress"))
        if progress:
            job.progress = max(0.0, min(1.0, progress))
        for attr, key in (
            ("duration_s", "printDuration"),
            ("weight_g", "estimatedRightWeight"),
            ("length_mm", "estimatedRightLen"),
        ):
            value = _number(detail.get(key))
            if value:
                setattr(job, attr, value)
        layer = _number(detail.get("printLayer"))
        total = _number(detail.get("targetPrintLayer"))
        if layer:
            job.layer = int(layer)
        if total:
            job.total_layers = int(total)

    def _finish(self, state: str, detail: Mapping[str, Any]) -> dict[str, Any]:
        job = self.job
        status = self._report(state, detail, message=job.error if job else None)
        self.job = None
        self.last_state = "standby"
        return status

    def _report(
        self, state: str, detail: Mapping[str, Any], *, message: str | None = None
    ) -> dict[str, Any]:
        self.last_state = state
        job = self.job
        print_stats: dict[str, Any] = {"state": state, "message": message or ""}
        progress = 0.0
        if job is not None:
            done = state == "complete"
            progress = 1.0 if done else job.progress
            # Slicer estimates for the whole job, scaled to what was printed.
            share = 1.0 if done else job.progress
            print_stats.update(
                {
                    "filename": job.filename,
                    "external_display_name": job.filename,
                    "print_duration": job.duration_s,
                    "external_current_layer": job.layer,
                    "external_total_layers": job.total_layers,
                }
            )
            if state in {"complete", "cancelled", "error"}:
                if job.length_mm:
                    print_stats["filament_used"] = job.length_mm * share
                if job.weight_g:
                    print_stats["filament_used_g"] = round(job.weight_g * share, 2)
        status: dict[str, Any] = {
            "print_stats": print_stats,
            "virtual_sdcard": {"progress": progress},
            "extruder": {
                "temperature": _number(detail.get("rightTemp")),
                "target": _number(detail.get("rightTargetTemp")),
            },
            "heater_bed": {
                "temperature": _number(detail.get("platTemp")),
                "target": _number(detail.get("platTargetTemp")),
            },
        }
        slots = material_slots(detail)
        if slots:
            status["material_slots"] = slots
        return status


def material_slots(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    """IFS slots as reported. ``currentSlot`` is unreliable and is ignored."""

    station = detail.get("matlStationInfo")
    if not detail.get("hasMatlStation") or not isinstance(station, Mapping):
        return []
    slots: list[dict[str, Any]] = []
    for info in station.get("slotInfos") or []:
        if not isinstance(info, Mapping) or info.get("slotId") is None:
            continue
        slot_id = info["slotId"]
        loaded = bool(info.get("hasFilament"))
        slots.append(
            {
                "slot_key": f"ifs{slot_id}",
                "label": f"IFS slot {slot_id}",
                "tool_key": "tool0",
                "state": "loaded" if loaded else "empty",
                "material_type": (info.get("materialName") or None) if loaded else None,
                "color_hex": (info.get("materialColor") or None) if loaded else None,
            }
        )
    return slots


# One tracker per printer, kept across the client rebuilds the hub performs on
# every reconnect. ponytail: process-local, so a restart forgets an in-flight
# cancel/error; the next `printing` poll re-adopts the job by filename.
_TRACKERS: dict[tuple[str, str], FlashforgeJobTracker] = {}


class FlashforgeClient:
    """Read-only client for the Flashforge HTTP API on port 8898."""

    capabilities = FLASHFORGE_CAPABILITIES

    def __init__(
        self,
        config: FlashforgeConfig,
        *,
        timeout: float = 10.0,
        poll_interval: float = POLL_INTERVAL_S,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        self.config = config
        self.base_url = base_url_for(config.host)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.transport = transport
        self._http_client_factory = http_client_factory or httpx.AsyncClient
        self.tracker = _TRACKERS.setdefault(
            (self.base_url, config.serial), FlashforgeJobTracker()
        )

    async def _post(self, path: str) -> dict[str, Any]:
        body = {"serialNumber": self.config.serial, "checkCode": self.config.check_code}
        try:
            async with self._http_client_factory(
                base_url=self.base_url, timeout=self.timeout, transport=self.transport
            ) as client:
                response = await client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise FlashforgeError(
                "flashforge_timeout", code="provider_timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise FlashforgeError(
                f"flashforge_unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise FlashforgeError(f"flashforge_http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise FlashforgeError("flashforge_invalid_json") from exc
        if not isinstance(payload, dict):
            raise FlashforgeError("flashforge_invalid_json")
        if payload.get("code") != 0:
            # A wrong serial or check code is the only rejection seen in practice.
            raise FlashforgeError(
                f"flashforge_rejected: {payload.get('message') or payload.get('code')}",
                code="provider_authentication_failed",
            )
        return payload

    async def detail(self) -> dict[str, Any]:
        detail = (await self._post("/detail")).get("detail")
        if not isinstance(detail, dict):
            raise FlashforgeError("flashforge_invalid_json")
        return detail

    async def info(self) -> dict[str, Any]:
        detail = await self.detail()
        return {
            "result": {
                "provider": "flashforge",
                "host": self.base_url,
                "name": detail.get("name"),
                "firmware": detail.get("firmwareVersion"),
                "nozzle": detail.get("nozzleModel"),
            }
        }

    async def server_info(self) -> dict[str, Any]:
        return await self.info()

    async def query_status(self) -> dict[str, Any]:
        return {"result": {"status": self.tracker.observe(await self.detail())}}

    async def query_snapshot(self) -> PrinterSnapshot:
        return PrinterSnapshot.from_legacy_payload(await self.query_status())

    async def subscribe_status(
        self,
        on_status: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            await on_status(self.tracker.observe(await self.detail()))
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def subscribe_snapshots(
        self,
        on_snapshot: SnapshotCallback,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        async def convert(status: dict[str, Any]) -> None:
            await on_snapshot(PrinterSnapshot.from_legacy_payload(status))

        await self.subscribe_status(convert, stop_event=stop_event)

    @staticmethod
    def _unsupported() -> Never:
        raise FlashforgeError(
            "operation_not_supported_for_provider",
            code="operation_not_supported_for_provider",
        )

    async def server_config(self) -> dict[str, Any]:
        self._unsupported()

    async def printer_config(self) -> dict[str, Any]:
        self._unsupported()

    async def list_files(self) -> list[Mapping[str, Any]]:
        self._unsupported()

    async def upload(self, local_path: Path, remote_filename: str) -> dict[str, Any]:
        del local_path, remote_filename
        self._unsupported()

    async def delete_file(self, remote_filename: str) -> dict[str, Any]:
        del remote_filename
        self._unsupported()

    async def start(self, remote_filename: str) -> dict[str, Any]:
        del remote_filename
        self._unsupported()

    async def pause(self) -> dict[str, Any]:
        self._unsupported()

    async def resume(self) -> dict[str, Any]:
        self._unsupported()

    async def cancel(self) -> dict[str, Any]:
        self._unsupported()

    async def run_gcode(self, script: str) -> dict[str, Any]:
        del script
        self._unsupported()

    async def emergency_stop(self) -> dict[str, Any]:
        self._unsupported()


class FlashforgeFactory:
    provider_id = ProviderId.FLASHFORGE
    capabilities = FLASHFORGE_CAPABILITIES

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    def build(self, config: PrinterConfig) -> PrinterClient:
        if not isinstance(config, FlashforgeConfig):
            raise ProviderError(
                "provider_config_mismatch", code="provider_config_mismatch"
            )
        return FlashforgeClient(
            config, timeout=self._timeout, transport=self._transport
        )
