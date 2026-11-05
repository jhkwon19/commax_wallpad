"""월패드 최신 방문자 사진과 HA 비공개 아카이브를 관리한다."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import CommaxRouterClient
from .call_diagnostics import DiagnosticJournal, PhotoObservation, file_id
from .const import (
    VISITOR_ARCHIVE_BATCH_SIZE,
    VISITOR_CALL_FAST_POLL_DURATION_SECONDS,
    VISITOR_CALL_FAST_SCAN_INTERVAL_SECONDS,
    VISITOR_SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

_QUERY_HELPER = "/root/wallpad-visitor-query.sh 2>&1"
_LIST_HELPER = "/root/wallpad-visitor-list.sh 2>&1"
_IMAGE_HELPER = "/root/wallpad-visitor-image.sh"
_FILENAME_RE = re.compile(
    r"^(?P<mac>[0-9a-fA-F]{12})-(?P<dong>\d+)-(?P<ho>\d+)-"
    r"(?P<uid>[0-9a-fA-F]+)-(?P<date>\d{4}-\d{2}-\d{2})-"
    r"(?P<time>\d{2}-\d{2}-\d{2})-(?P<flags>\d+(?:-\d+)*)\.jpg$"
)
_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"
_STATE_FILE = ".archive_state.json"


def _metadata_from_filename(filename: str) -> dict[str, str]:
    """검증된 파일명에서 공개 가능한 방문자 메타데이터를 만든다."""
    match = _FILENAME_RE.fullmatch(filename)
    if match is None:
        raise ValueError(f"invalid visitor filename: {filename}")
    parts = match.groupdict()
    flags = parts["flags"]
    flag_parts = flags.split("-")
    location_code = flag_parts[2] if len(flag_parts) > 2 else None
    return {
        "filename": filename,
        "uid": parts["uid"],
        "captured_at": f'{parts["date"]}T{parts["time"].replace("-", ":")}',
        "flags": flags,
        "location": {"0": "세대현관", "1": "공동현관"}.get(
            location_code, "알 수 없음"
        ),
    }


def _parse_metadata(body: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in body.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"filename", "uid", "captured_at", "flags", "marker"}:
            parsed[key] = value.strip()
    required = {"filename", "uid", "captured_at", "flags", "marker"}
    missing = required - parsed.keys()
    if missing:
        raise ValueError(
            f"missing visitor metadata fields: {sorted(missing)} "
            f"(helper output: {body[:300]!r})"
        )
    filename_data = _metadata_from_filename(parsed["filename"])
    for key in ("uid", "captured_at", "flags"):
        if parsed[key] != filename_data[key]:
            raise ValueError(
                f"visitor {key} does not match filename "
                f"(helper output: {body[:300]!r})"
            )
    parsed["location"] = filename_data["location"]
    return parsed


def _parse_captured_at(value: str, hass: HomeAssistant) -> datetime:
    captured = datetime.fromisoformat(value)
    timezone = dt_util.get_time_zone(hass.config.time_zone) or dt_util.DEFAULT_TIME_ZONE
    return captured.replace(tzinfo=timezone)


class VisitorCoordinator(DataUpdateCoordinator[dict]):
    """최신 사진을 폴링하고 누락 사진을 인증 전용 저장소에 보관한다."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CommaxRouterClient,
        entry_id: str,
        diagnostics: DiagnosticJournal | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="commax_wallpad_visitor",
            update_interval=timedelta(seconds=VISITOR_SCAN_INTERVAL_SECONDS),
        )
        self._client = client
        self.entry_id = entry_id
        self.diagnostics = diagnostics
        self._observation: PhotoObservation | None = None
        self._observation_timer: asyncio.TimerHandle | None = None
        self.archive_dir = Path(
            hass.config.path("commax_wallpad", "visitor", entry_id)
        )
        self._legacy_dir = Path(hass.config.path("www", "commax_wallpad", "visitor"))
        self._skip_archive_once = True
        self._archive_prepared = False
        self._archive_names_cache: frozenset[str] | None = None
        self._archive_items_cache: list[dict[str, str]] | None = None
        self._archive_generation = 0
        self._fast_poll_until: float | None = None
        self._fast_poll_baseline: str | None = None
        self._archive_state: dict = {
            "initial_complete": False,
            "baseline": None,
        }

    async def async_start_call_fast_poll(
        self, *, started_epoch: float | None = None, place: str = "", display_name: str = ""
    ) -> None:
        """실제 벨 호출 뒤 새 JPEG가 생길 때까지만 1초 폴링한다."""
        self._fast_poll_until = (
            time.monotonic() + VISITOR_CALL_FAST_POLL_DURATION_SECONDS
        )
        self._fast_poll_baseline = (
            self.data.get("filename") if self.data is not None else None
        )
        self.stop_observation("superseded")
        if self.diagnostics:
            self._observation = PhotoObservation(
                self.diagnostics, started_epoch=started_epoch or time.time(),
                place=place, display_name=display_name, baseline=self._fast_poll_baseline,
            )
            self._observation_timer = asyncio.get_running_loop().call_later(
                VISITOR_CALL_FAST_POLL_DURATION_SECONDS, self._observation_deadline
            )
        self.update_interval = timedelta(
            seconds=VISITOR_CALL_FAST_SCAN_INTERVAL_SECONDS
        )
        await self.async_request_refresh()

    def stop_observation(self, reason: str = "integration_unloaded") -> None:
        if self._observation_timer:
            self._observation_timer.cancel()
            self._observation_timer = None
        if self._observation:
            self._observation.finish(reason)

    def _observation_deadline(self) -> None:
        self.stop_observation("deadline")
        self._stop_call_fast_poll()

    def _diagnostic(self, kind: str, **fields) -> None:
        if self.diagnostics:
            fields.setdefault("call_id", self._observation.call_id if self._observation else None)
            self.diagnostics.emit(kind, **fields)

    def _diagnostic_error(self, stage: str, exc: Exception, filename: str | None = None) -> None:
        self._diagnostic("photo_stage_error", stage=stage, error_type=type(exc).__name__,
                         exit_status=getattr(exc, "exit_status", None), file_id=file_id(filename))

    def _call_fast_poll_active(self) -> bool:
        return (
            self._fast_poll_until is not None
            and time.monotonic() < self._fast_poll_until
        )

    def _stop_call_fast_poll(self) -> None:
        self._fast_poll_until = None
        self._fast_poll_baseline = None
        self.update_interval = timedelta(seconds=VISITOR_SCAN_INTERVAL_SECONDS)

    def _previous_archive_status(self) -> dict:
        """빠른 사진 확인 중 무거운 전체 파일 목록 조회를 건너뛴다."""
        previous = self.data or {}
        return {
            "archive_count": previous.get("archive_count", 0),
            "archive_pending": previous.get("archive_pending"),
            "archive_api": f"commax_wallpad/visitor/{self.entry_id}",
            "archive_last_sync": previous.get("archive_last_sync"),
            "archive_error": previous.get("archive_error"),
            "new_visitors": [],
        }

    async def _async_fetch_image(self, filename: str) -> bytes:
        image = await self._client.exec_bytes(
            f"{_IMAGE_HELPER} {shlex.quote(filename)}", timeout=20
        )
        if not image.startswith(_JPEG_SOI) or not image.endswith(_JPEG_EOI):
            raise ValueError(f"incomplete JPEG: {filename}")
        return image

    def _prepare_archive(self) -> None:
        """기존 공개 아카이브를 비공개 경로로 이동하고 상태를 읽는다."""
        if self._archive_prepared:
            return
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        if self._legacy_dir.is_dir():
            for path in self._legacy_dir.glob("*.jpg"):
                if _FILENAME_RE.fullmatch(path.name):
                    target = self.archive_dir / path.name
                    if target.exists():
                        path.unlink()
                    else:
                        path.replace(target)
            # 공개 인덱스에는 사진 파일명과 방문시각도 있으므로 함께 제거한다.
            for name in ("index.html", "index.html.part"):
                path = self._legacy_dir / name
                if path.exists():
                    path.unlink()
            # 비정상 종료 때 남은 불완전 JPEG도 공개 경로에 두지 않는다.
            for path in self._legacy_dir.glob("*.jpg.part"):
                path.unlink()
            try:
                self._legacy_dir.rmdir()
            except OSError:
                pass

        state_path = self.archive_dir / _STATE_FILE
        if state_path.exists():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded.get("initial_complete"), bool) and (
                    loaded.get("baseline") is None
                    or isinstance(loaded.get("baseline"), list)
                ):
                    self._archive_state = loaded
            except (OSError, ValueError):
                _LOGGER.warning("Visitor archive state is invalid; rebuilding baseline")
        self._archive_prepared = True

    def _save_archive_state(self) -> None:
        temporary = self.archive_dir / f"{_STATE_FILE}.part"
        temporary.write_text(
            json.dumps(self._archive_state, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(self.archive_dir / _STATE_FILE)

    def _local_archive_names(self) -> set[str]:
        self._prepare_archive()
        if self._archive_names_cache is None:
            self._archive_names_cache = frozenset(
                path.name
                for path in self.archive_dir.glob("*.jpg")
                if _FILENAME_RE.fullmatch(path.name)
            )
        return set(self._archive_names_cache)

    def _write_archive_image(self, filename: str, image: bytes) -> None:
        target = self.archive_dir / filename
        temporary = target.with_suffix(".jpg.part")
        temporary.write_bytes(image)
        temporary.replace(target)
        names = set(self._archive_names_cache or ())
        names.add(filename)
        self._archive_names_cache = frozenset(names)
        self._archive_items_cache = None
        self._archive_generation += 1

    def archive_version(self) -> str:
        """디렉터리 재정렬 없이 카드가 비교할 작은 버전 문자열을 반환한다."""
        return f"{len(self._local_archive_names())}:{self._archive_generation}"

    def archive_page(
        self,
        page: int,
        per_page: int,
        date_filter: str | None,
        location_filter: str | None,
    ) -> dict:
        """인증 HTTP API에 제공할 페이지 단위 목록을 만든다."""
        names = self._local_archive_names()
        if self._archive_items_cache is None:
            self._archive_items_cache = sorted(
                (_metadata_from_filename(name) for name in names),
                key=lambda item: item["captured_at"],
                reverse=True,
            )
        items = self._archive_items_cache
        if date_filter:
            items = [item for item in items if item["captured_at"][:10] == date_filter]
        if location_filter:
            items = [item for item in items if item["location"] == location_filter]
        total = len(items)
        start = (page - 1) * per_page
        selected = items[start : start + per_page]
        public_items = [
            {
                "filename": item["filename"],
                "captured_at": item["captured_at"],
                "location": item["location"],
            }
            for item in selected
        ]
        return {
            "items": public_items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "has_more": start + len(selected) < total,
            "version": self.archive_version(),
        }

    async def _async_archive_status(self) -> dict:
        try:
            local = await self.hass.async_add_executor_job(self._local_archive_names)
        except Exception as exc:
            self._diagnostic_error("archive_local_index", exc)
            raise
        return {
            "archive_count": len(local),
            "archive_pending": None,
            "archive_api": f"commax_wallpad/visitor/{self.entry_id}",
            "archive_last_sync": None,
            "archive_error": None,
            "new_visitors": [],
        }

    async def _async_sync_archive(self) -> dict:
        try:
            listing = await self._client.exec(_LIST_HELPER, timeout=15)
        except Exception as exc:
            self._diagnostic_error("archive_list", exc)
            raise
        remote: set[str] = set()
        for line in listing.splitlines():
            filename = line.strip()
            if filename:
                _metadata_from_filename(filename)
                remote.add(filename)

        local = await self.hass.async_add_executor_job(self._local_archive_names)
        state_changed = False
        if self._archive_state.get("baseline") is None and not self._archive_state[
            "initial_complete"
        ]:
            self._archive_state["baseline"] = sorted(remote)
            state_changed = True
        baseline = set(self._archive_state.get("baseline") or [])

        missing = sorted(
            remote - local,
            key=lambda name: _metadata_from_filename(name)["captured_at"],
            reverse=True,
        )
        copied: list[dict[str, str]] = []
        for filename in missing[:VISITOR_ARCHIVE_BATCH_SIZE]:
            stage = "archive_transfer"
            try:
                image = await self._async_fetch_image(filename)
                stage = "archive_save"
                await self.hass.async_add_executor_job(
                    self._write_archive_image, filename, image
                )
                metadata = _metadata_from_filename(filename)
                if self._archive_state["initial_complete"] or filename not in baseline:
                    copied.append(metadata)
                local.add(filename)
                self._diagnostic("photo_stage_ok", stage="archive_save",
                                 file_id=file_id(filename), bytes=len(image))
            except Exception as err:  # noqa: BLE001 - 다른 사진은 계속 처리한다
                self._diagnostic_error(stage, err, filename)
                _LOGGER.warning("Visitor archive copy failed for %s: %s", filename, err)

        pending = len(remote - local)
        if not self._archive_state["initial_complete"] and pending == 0:
            self._archive_state = {"initial_complete": True, "baseline": None}
            state_changed = True
        if state_changed:
            await self.hass.async_add_executor_job(self._save_archive_state)

        return {
            "archive_count": len(local),
            "archive_pending": pending,
            "archive_api": f"commax_wallpad/visitor/{self.entry_id}",
            "archive_last_sync": dt_util.utcnow(),
            "archive_error": None,
            "new_visitors": copied,
        }

    async def _async_update_data(self) -> dict:
        fast_polling = self._call_fast_poll_active()
        if self._fast_poll_until is not None and not fast_polling:
            self._stop_call_fast_poll()
        stage = "metadata_query"
        filename = None
        if self._observation:
            self._observation.begin_check()
        try:
            metadata = _parse_metadata(await self._client.exec(_QUERY_HELPER, timeout=10))
            filename = metadata["filename"]
            captured_datetime = _parse_captured_at(metadata["captured_at"], self.hass)
            if self._observation and self._observation.observe(metadata, captured_datetime.timestamp()):
                if self._observation_timer:
                    self._observation_timer.cancel()
                    self._observation_timer = None
                self._stop_call_fast_poll()
            filename_changed = (
                self.data is None or self.data.get("filename") != filename
            )
            if not filename_changed:
                latest = {
                    **self.data,
                    "marker": metadata["marker"],
                    "last_checked_at": dt_util.utcnow(),
                }
            else:
                self._diagnostic("photo_stage_ok", stage="metadata_query", file_id=file_id(filename),
                                 captured_at=metadata["captured_at"])
                stage = "latest_transfer"
                image = await self._async_fetch_image(filename)
                self._diagnostic("photo_stage_ok", stage=stage, file_id=file_id(filename), bytes=len(image))
                latest = {
                    **metadata,
                    "captured_datetime": captured_datetime,
                    "image": image,
                    "image_last_updated": captured_datetime,
                    "last_checked_at": dt_util.utcnow(),
                }
        except Exception as err:  # noqa: BLE001
            self._diagnostic_error(stage, err, filename)
            if self._observation:
                self._observation.error(stage, err)
            raise UpdateFailed(
                "Visitor update failed; check USB ADB and visitor helpers: " f"{err}"
            ) from err

        # 호출 직후 발견한 첫 새 사진에서 빠른 모드를 끝낸다. 사진 생성에
        # 실패하거나 호출에 사진이 없더라도 20초 안전 제한으로 자동 복귀한다.
        if fast_polling and (
            (filename_changed and filename != self._fast_poll_baseline)
            or not self._call_fast_poll_active()
        ):
            self._stop_call_fast_poll()

        if self._skip_archive_once:
            self._skip_archive_once = False
            archive = await self._async_archive_status()
        elif fast_polling and not filename_changed:
            archive = self._previous_archive_status()
        else:
            try:
                archive = await self._async_sync_archive()
            except Exception as err:  # noqa: BLE001 - 최신 사진은 유지한다
                self._diagnostic_error("archive_sync", err)
                _LOGGER.warning("Visitor archive sync failed: %s", err)
                archive = await self._async_archive_status()
                archive["archive_error"] = str(err)
        return {**latest, **archive}
