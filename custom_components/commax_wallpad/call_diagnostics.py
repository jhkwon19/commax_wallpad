"""Bounded, private diagnostic journal; never sends commands to the wallpad."""
from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import json
import logging
from pathlib import Path
import time
from uuid import uuid4

EVENT_TYPE = "commax_wallpad_diagnostic"
_LOGGER = logging.getLogger(__name__)
MAX_BYTES = 512 * 1024
HISTORY_SIZE = 256
# Never accept exception messages, commands, credentials, raw filenames or images.
FIELDS = {"call_id", "place", "display_name", "source_epoch", "stage", "error_type",
          "exit_status", "file_id", "captured_at", "result", "reason", "checks",
          "errors", "elapsed_ms", "bytes", "reconnect_count", "connected"}


def file_id(filename: str | None) -> str | None:
    return hashlib.sha256(filename.encode()).hexdigest()[:16] if filename else None


class DiagnosticJournal:
    """Event-loop owned history with one bounded asynchronous disk writer."""
    def __init__(self, hass, entry_id):
        self.hass = hass
        self.entry_id = entry_id
        self.path = Path(hass.config.path("commax_wallpad", "diagnostics", entry_id, "events.jsonl"))
        self.session_id = uuid4().hex
        self.sequence = 0
        self.history = deque(maxlen=HISTORY_SIZE)
        self.queue = asyncio.Queue(maxsize=HISTORY_SIZE)
        self.worker = None
        self.dropped_writes = 0
        self.storage_error_type = None
        self.closed = False

    def _load(self):
        rows = deque(maxlen=HISTORY_SIZE)
        for path in (self.path.with_suffix(".jsonl.1"), self.path):
            if not path.exists():
                continue
            # Bound reading even if a file was externally replaced with an oversized one.
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - MAX_BYTES))
                if stream.tell():
                    stream.readline()
                for line in stream:
                    try:
                        record = json.loads(line)
                        if record.get("schema") == 1 and record.get("entry_id") == self.entry_id:
                            rows.append(record)
                    except (ValueError, AttributeError):
                        continue
        return list(rows)

    async def async_start(self):
        try:
            self.history.extend(await self.hass.async_add_executor_job(self._load))
        except OSError as exc:
            self.storage_error_type = type(exc).__name__
        self.worker = self.hass.async_create_background_task(self._writer(), name="commax_diagnostic_writer")
        self.emit("journal_started")

    def emit(self, kind, **fields):
        if self.closed:
            return
        self.sequence += 1
        record = {"schema": 1, "id": f"{self.session_id}:{self.sequence}",
                  "entry_id": self.entry_id, "session_id": self.session_id,
                  "sequence": self.sequence, "occurred": time.time(), "kind": kind}
        record.update({key: value for key, value in fields.items() if key in FIELDS and
                       (value is None or isinstance(value, (str, int, float, bool)))})
        # Each bounded field is selected by our own call sites; arbitrary errors are excluded.
        for key, value in list(record.items()):
            if isinstance(value, str):
                record[key] = value[:160]
        self.history.append(record)
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped_writes += 1
        self.hass.bus.async_fire(EVENT_TYPE, record)

    def _append(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for row in rows:
            line = (json.dumps(row, separators=(",", ":")) + "\n").encode()
            if self.path.exists() and self.path.stat().st_size + len(line) > MAX_BYTES:
                self.path.replace(self.path.with_suffix(".jsonl.1"))
            # O_CREAT mode is private from the first write, independent of HA's umask.
            import os
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "ab") as stream:
                stream.write(line)

    async def _writer(self):
        while True:
            first = await self.queue.get()
            if first is None:
                self.queue.task_done()
                return
            batch = [first]
            while len(batch) < 32 and not self.queue.empty():
                # close only enqueues sentinel after join, so none cannot occur here.
                batch.append(self.queue.get_nowait())
            try:
                await self.hass.async_add_executor_job(self._append, batch)
                self.storage_error_type = None
            except OSError as exc:
                if self.storage_error_type != type(exc).__name__:
                    _LOGGER.warning("Diagnostic journal write failed: %s", type(exc).__name__)
                self.storage_error_type = type(exc).__name__
                self.dropped_writes += len(batch)
            finally:
                for _ in batch:
                    self.queue.task_done()

    def snapshot(self):
        return {"entry_id": self.entry_id, "session_id": self.session_id,
                "sequence": self.sequence, "history_limit": HISTORY_SIZE,
                "dropped_writes": self.dropped_writes, "storage_error_type": self.storage_error_type,
                "records": list(self.history)}

    async def async_close(self):
        self.emit("journal_stopped")
        self.closed = True
        if self.worker:
            await self.queue.join()
            await self.queue.put(None)
            await self.worker
            self.worker = None


class PhotoObservation:
    """A bounded observation window, not a verdict about black video or causality."""
    def __init__(self, journal, *, started_epoch, place, display_name, baseline):
        self.journal = journal
        self.call_id = uuid4().hex
        self.started_epoch = started_epoch
        self.place = place
        self.display_name = display_name
        self.baseline = baseline
        self.started = time.monotonic()
        self.checks = 0
        self.errors = 0
        self.finished = False
        self.pending = False
        self.last_filename = None
        self.journal.emit("call_observation_started", call_id=self.call_id, place=place,
                          display_name=display_name, source_epoch=started_epoch,
                          file_id=file_id(baseline))

    def begin_check(self):
        if not self.finished:
            self.pending = True

    def error(self, stage, exc):
        if self.finished:
            return
        self.pending = False
        if stage == "metadata_query" and getattr(exc, "exit_status", None) == 3:
            self.checks += 1
            self.journal.emit("source_photo_absent", call_id=self.call_id, stage=stage,
                              reason="empty_photo_directory")
            return
        self.errors += 1
        self.journal.emit("photo_check_error", call_id=self.call_id, stage=stage,
                          error_type=type(exc).__name__, exit_status=getattr(exc, "exit_status", None))

    def observe(self, metadata, captured_epoch):
        if self.finished:
            return False
        self.pending = False
        self.checks += 1
        filename = metadata["filename"]
        expected = {"2": "세대현관", "5": "공동현관"}.get(self.place)
        # A changed filename alone may be an older, delayed photo. Compare capture time
        # and household/lobby category too. Lobby photo filenames don't identify a gate.
        reason = ("unchanged_baseline" if filename == self.baseline else
                  "capture_predates_call" if captured_epoch < self.started_epoch - 1 else
                  "future_capture_time" if captured_epoch > time.time() + 2 else
                  "category_mismatch" if expected and metadata.get("location") != expected else
                  "capture_time_and_category_match_gate_unverified")
        fresh = reason == "capture_time_and_category_match_gate_unverified"
        if filename != self.last_filename:
            self.journal.emit("photo_check_observed", call_id=self.call_id,
                              file_id=file_id(filename), captured_at=metadata["captured_at"], reason=reason)
            self.last_filename = filename
        if reason == "future_capture_time":
            self.errors += 1
        if fresh:
            self.journal.emit("source_photo_observed", call_id=self.call_id,
                              file_id=file_id(filename), captured_at=metadata["captured_at"],
                              reason="capture_time_and_category_match_gate_unverified")
            self.finish("new_photo_observed")
            return True
        return False

    def finish(self, reason="deadline"):
        if self.finished:
            return
        self.finished = True
        result = reason
        if reason == "deadline":
            result = "observation_incomplete" if self.errors or self.pending or not self.checks else "no_new_photo_observed"
        self.journal.emit("call_photo_result", call_id=self.call_id, result=result,
                          reason=reason, checks=self.checks, errors=self.errors,
                          elapsed_ms=round((time.monotonic() - self.started) * 1000))
