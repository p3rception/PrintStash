"""Background worker: watch external-library folders for real-time changes.

For each external library whose ``watch_mode`` resolves to "watch" (see
``external_library.should_watch``), we keep one ``watchfiles.awatch`` task.
A burst of filesystem events is debounced and then simply triggers the existing
``external_library.scan_library`` reconcile — the watcher never re-implements
indexing, so all of the scan's safety guards (abort on unmounted/empty root, no
mass-delete) apply unchanged.

Watching only works on local filesystems; on network mounts (NFS/SMB/CIFS) the
kernel does not deliver inotify events. ``AUTO`` libraries on a network root are
left to their scheduled scan. When the user forces ``EVENTS`` on a non-local
root we fall back to watchfiles' own stat-polling (``force_polling``) so the
feature still works, just less efficiently.

A failed watcher never blocks startup or crashes the app: errors are logged and
the library falls back to schedule-only.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional

from sqlmodel import select

import app.modules.sources.root_binding as source_root_binding
from app.core.logging import get_logger
from app.db.models import ExternalLibrary, JobKind
from app.db.session import get_session_factory
from app.modules.administration.runtime_config import external_libraries_enabled
from app.modules.sources import external_library

logger = get_logger(__name__)

# Coalesce a burst of file events for this long before triggering a reconcile.
_DEBOUNCE_S = 10.0
# How often the supervisor re-syncs running watchers against the DB.
_SUPERVISOR_INTERVAL_S = 45.0


class LibraryWatcher:
    def __init__(self) -> None:
        self.tasks: Dict[int, asyncio.Task] = {}
        self.stop_events: Dict[int, asyncio.Event] = {}
        # The root currently being watched per library (to detect path edits).
        self.watched_roots: Dict[int, str] = {}
        self._pending: Dict[int, asyncio.Task] = {}
        self._supervisor: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._refresh_requested = asyncio.Event()
        self._lock = asyncio.Lock()

    # -- lifecycle --

    async def start_all(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.refresh()
        if self._supervisor is None:
            self._supervisor = asyncio.create_task(
                self._supervise(), name="library-watcher-supervisor"
            )

    async def stop_all(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            self._supervisor = None
        async with self._lock:
            ids = list(self.tasks.keys())
        for lib_id in ids:
            await self._stop_watcher(lib_id)

    async def _supervise(self) -> None:
        while True:
            try:
                try:
                    await asyncio.wait_for(
                        self._refresh_requested.wait(), _SUPERVISOR_INTERVAL_S
                    )
                except TimeoutError:
                    pass
                self._refresh_requested.clear()
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("library watcher supervisor tick failed")

    def request_refresh(self) -> None:
        """Ask the supervisor to reconcile now (callable from any thread).

        A configuration change should not wait for the supervisor's interval,
        and it must not start its own background task: the supervisor owns
        every watcher, so it is the one that reconciles them.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._refresh_requested.set)

    async def refresh(self) -> None:
        """Reconcile running watchers with current library config.

        Starts watchers that should run, stops those that shouldn't, and restarts
        any whose root path changed. Safe to call repeatedly (API edits, ticks).
        """
        from app.runtime.maintenance import restore_in_progress

        if restore_in_progress():
            return
        desired = await asyncio.to_thread(self._compute_desired)

        async with self._lock:
            running = set(self.tasks.keys())
        desired_ids = set(desired.keys())

        for lib_id in running - desired_ids:
            await self._stop_watcher(lib_id)

        for lib_id, (root, force_polling) in desired.items():
            if lib_id in self.tasks and self.watched_roots.get(lib_id) != root:
                await self._stop_watcher(lib_id)  # root changed → restart
            if lib_id not in self.tasks:
                await self._start_watcher(lib_id, root, force_polling)

    def _compute_desired(self) -> Dict[int, tuple[str, bool]]:
        """{library_id: (root, force_polling)} for libraries that should be watched.

        Also persists the freshly detected ``fs_kind`` so the UI can explain why
        watching is or isn't active. Runs in a worker thread (sync DB + /proc IO).
        """
        desired: Dict[int, tuple[str, bool]] = {}
        with get_session_factory().session() as session:
            if not external_libraries_enabled(session):
                return desired
            libs = session.exec(
                select(ExternalLibrary).where(ExternalLibrary.enabled == True)  # noqa: E712
            ).all()
            for lib in libs:
                if lib.id is None:
                    continue
                # Watching an unbound, missing, or replacement root would
                # observe a namespace that is not proven to belong to this
                # library.  Scan itself repeats the check before mutation.
                binding_state, _ = source_root_binding.root_binding_state(lib)
                if binding_state != "bound":
                    continue
                fs_kind = external_library.detect_fs_kind(lib.root_path)
                if lib.fs_kind != fs_kind:
                    lib.fs_kind = fs_kind
                    session.add(lib)
                if external_library.should_watch(lib, fs_kind):
                    desired[lib.id] = (lib.root_path, fs_kind != "local")
            session.commit()
        return desired

    async def _start_watcher(
        self, library_id: int, root: str, force_polling: bool
    ) -> None:
        async with self._lock:
            if library_id in self.tasks:
                return
            stop = asyncio.Event()
            self.stop_events[library_id] = stop
            self.watched_roots[library_id] = root
            self.tasks[library_id] = asyncio.create_task(
                self._run_watcher(library_id, root, force_polling, stop),
                name=f"library-watch-{library_id}",
            )
        logger.info(
            "watching external library %s at %s (polling=%s)",
            library_id,
            root,
            force_polling,
        )

    async def _stop_watcher(self, library_id: int) -> None:
        async with self._lock:
            stop = self.stop_events.pop(library_id, None)
            task = self.tasks.pop(library_id, None)
            self.watched_roots.pop(library_id, None)
            pending = self._pending.pop(library_id, None)
        if stop:
            stop.set()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("library watcher exit error for %s", library_id)
        if pending:
            pending.cancel()

    # -- worker --

    async def _run_watcher(
        self, library_id: int, root: str, force_polling: bool, stop: asyncio.Event
    ) -> None:
        from watchfiles import awatch

        try:
            try:
                async for _changes in awatch(
                    root,
                    recursive=True,
                    stop_event=stop,
                    force_polling=force_polling,
                ):
                    self._schedule_scan(library_id)
            except PermissionError:
                # Some NAS share roots refuse an inotify watch to a uid that can
                # still read them (UGOS ACLs); stat-polling works there.
                if force_polling:
                    raise
                logger.warning(
                    "events refused for library %s at %s; polling instead",
                    library_id,
                    root,
                )
                async for _changes in awatch(
                    root, recursive=True, stop_event=stop, force_polling=True
                ):
                    self._schedule_scan(library_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never crash the app over a watcher failure — fall back to schedule.
            logger.exception(
                "watcher for library %s failed; falling back to scheduled scans",
                library_id,
            )

    # -- debounced scan --

    def _schedule_scan(self, library_id: int) -> None:
        existing = self._pending.get(library_id)
        if existing and not existing.done():
            existing.cancel()
        self._pending[library_id] = asyncio.create_task(
            self._debounced_scan(library_id)
        )

    async def _debounced_scan(self, library_id: int) -> None:
        """After a quiet period, ask for a scan; the scan Job does the work.

        The watcher never scans in-process. It records the request on the
        library and nudges the scan source, so a burst of changes becomes one
        Job, a change during a running scan becomes the next Job (the request
        is recorded after the running scan took the previous one), and the
        scan runs on whichever process listens to its lane.
        """
        try:
            await asyncio.sleep(_DEBOUNCE_S)
        except asyncio.CancelledError:
            return
        try:
            await asyncio.to_thread(self._request_scan, library_id)
        except Exception:
            logger.exception("watched scan request failed for library %s", library_id)

    @staticmethod
    def _request_scan(library_id: int) -> None:
        from app.modules.work import nudge

        with get_session_factory().scoped_session() as session:
            external_library.request_scan(session, library_id)
        nudge(JobKind.SOURCES_SCAN)
