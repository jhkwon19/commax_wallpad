"""세대 검침 수집기(HNA)의 계량기 누적 지침을 30분마다 읽는다.

월패드 데몬은 ttyS3(RS485)로 검침 수집기에 10초마다 값을 묻는다. 라우터
도우미(wallpad-meter-read.sh)는 그 응답을 읽는 데몬 스레드 하나에만 strace를
잠깐 붙여 유효한 프레임 하나를 받는 즉시 뗀다(최대 12초). 버스에는 아무것도
쓰지 않는다.

기존 기능에 영향을 주지 않도록 다음을 지킨다.

- 전용 SSH 연결을 쓴다. 방문자 사진/호출 감시/엘리베이터 연결의 잠금과
  대기열에 끼지 않는다.
- 엘리베이터 조회 잠금(lan1 브리지)을 쓰지 않는다. 이 경로는 USB ADB만 쓴다.
- 현관 호출 중이거나 호출 직후 빠른 사진 확인 중이면 그 회차를 건너뛴다.
  누적값이라 다음 회차에 그대로 따라잡는다.
- 실패해도 마지막 정상 값을 유지하고 다른 기기에 전파하지 않는다.

프레임 형식(2026-11-23 실측, 고지서·계량기 대조로 확정):
``f7 30 0f 81 19`` + 계량기 5종 x 5바이트 + XOR + SUM. 각 5바이트는 순간값
2바이트 + 누적값 3바이트(BCD, 소수점 한 자리)이며 순서는 수도, 가스, 전기,
온수, 열량이다. 순간값은 정확도가 낮아 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from functools import reduce

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import CommaxRouterClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

METER_HELPER = "/root/wallpad-meter-read.sh"
# 매시 :00:30, :30:30. HA 에너지 통계는 시간 단위라 정시 직후에 읽어야 사용량이
# 올바른 시간 막대에 들어간다.
METER_READ_MINUTES = (0, 30)
METER_READ_SECOND = 30
# 시작 직후의 방문자 첫 조회·호출 감시 연결과 겹치지 않게 조금 늦게 첫 값을 읽는다.
METER_STARTUP_DELAY_SECONDS = 90
# 정상: 1~12초. 데몬 재시작 뒤 스레드를 다시 찾으면 후보 최대 4개 x 12초.
METER_COMMAND_TIMEOUT_SECONDS = 75
# 줄어들거나 비정상적으로 큰 값은 이 횟수만큼 연속으로 같은 판단이 나와야
# 받아들인다(계량기 교체 등). 단발성 이상값이 에너지 통계를 망치지 않게 한다.
METER_SUSPECT_CONFIRMATIONS = 3
# 수동 조회(버튼·서비스) 연타나 자동화 반복으로 strace가 연달아 붙지 않게 한다.
# 수집기 응답 자체가 10초 주기이고 지침 단위가 0.1이라 이보다 촘촘해도 의미가 없다.
METER_MANUAL_MIN_INTERVAL_SECONDS = 60

FRAME_HEADER = bytes.fromhex("f7300f8119")
FRAME_LENGTH = 32
# 프레임 안 순서. 온수·열량은 이 세대에 없어 항상 0이므로 노출하지 않는다.
_FRAME_ORDER = ("water", "gas", "electricity", "warm_water", "heat")
METER_KEYS = ("gas", "water", "electricity")
# 시간당 최대 사용량(여유 포함). 가스: G2.5 계량기 Qmax 4 m³/h.
# 수도: 세대 최대 유량보다 넉넉히. 전기: 40A x 220V ≈ 8.8 kWh/h.
_MAX_PER_HOUR = {"gas": 5.0, "water": 3.0, "electricity": 10.0}


class MeterFrameError(ValueError):
    """검침 프레임이 형식·체크섬·BCD 검증을 통과하지 못했다."""


def _bcd(raw: bytes) -> int:
    digits = raw.hex()
    if not digits.isdigit():
        raise MeterFrameError(f"non-BCD digits: {digits}")
    return int(digits)


def meter_device_info(entry) -> DeviceInfo:
    """검침 센서와 수동 조회 버튼을 같은 'Commax 검침' 기기로 묶는다."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}-meter-reading")},
        name=f"Commax 검침 ({entry.data[CONF_HOST]})",
        manufacturer="Commax",
        model="CDP-1020MB HNA metering (RS485 observe)",
    )


def decode_meter_frame(frame_hex: str) -> dict[str, float]:
    """응답 프레임 하나를 계량기별 누적 지침으로 바꾼다."""
    try:
        frame = bytes.fromhex(frame_hex.strip())
    except ValueError as err:
        raise MeterFrameError("frame is not hex") from err
    if len(frame) != FRAME_LENGTH or frame[:5] != FRAME_HEADER:
        raise MeterFrameError("unexpected frame header or length")
    xor = reduce(lambda acc, byte: acc ^ byte, frame[:-2], 0)
    total = (sum(frame[:-2]) + xor) & 0xFF
    if xor != frame[-2] or total != frame[-1]:
        raise MeterFrameError("checksum mismatch")
    readings: dict[str, float] = {}
    for index, key in enumerate(_FRAME_ORDER):
        block = frame[5 + index * 5 : 10 + index * 5]
        _bcd(block[:2])  # 순간값도 BCD여야 정상 프레임이다(값 자체는 쓰지 않음).
        readings[key] = _bcd(block[2:]) / 10
    return {key: readings[key] for key in METER_KEYS}


def parse_helper_output(output: str) -> tuple[str, dict[str, float]]:
    """도우미 출력 ``frame <tid> <hex>``에서 스레드와 지침을 꺼낸다."""
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "frame":
            return parts[1], decode_meter_frame(parts[2])
    raise MeterFrameError("helper returned no frame")


class MeterCoordinator(DataUpdateCoordinator[dict]):
    """정해진 시각에만 검침을 읽고, 실패해도 마지막 정상 값을 유지한다."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CommaxRouterClient,
        visitor_coordinator,
        visitor_call_coordinator,
    ) -> None:
        # update_interval=None: HA의 자동 주기 갱신을 쓰지 않고 시계 기준으로만 읽는다.
        super().__init__(hass, _LOGGER, name="commax_wallpad_meter", update_interval=None)
        self._client = client
        self._visitor = visitor_coordinator
        self._visitor_call = visitor_call_coordinator
        self._read_lock = asyncio.Lock()
        self._unsubscribers: list = []
        self._accepted_monotonic: float | None = None
        self._last_manual_monotonic: float | None = None
        self._suspect_count = 0
        self.data = {
            **{key: None for key in METER_KEYS},
            "last_success": None,
            "last_attempt": None,
            "last_error": None,
            "last_skip_reason": None,
            "reader_thread": None,
        }

    async def _async_update_data(self) -> dict:
        """엔티티 생성용 스냅샷. 여기서는 라우터에 접속하지 않는다."""
        return dict(self.data)

    async def async_start(self) -> None:
        self._unsubscribers.append(
            async_track_time_change(
                self.hass,
                self._async_scheduled_read,
                minute=METER_READ_MINUTES,
                second=METER_READ_SECOND,
            )
        )
        self._unsubscribers.append(
            async_call_later(self.hass, METER_STARTUP_DELAY_SECONDS, self._async_scheduled_read)
        )

    async def async_stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        await self._client.close()

    @callback
    def _async_scheduled_read(self, _now=None) -> None:
        self.hass.async_create_task(self.async_read_once())

    def _skip_reason(self) -> str | None:
        call_data = getattr(self._visitor_call, "data", None) or {}
        if call_data.get("active"):
            return "visitor_call_active"
        fast_poll_active = getattr(self._visitor, "_call_fast_poll_active", None)
        if callable(fast_poll_active) and fast_poll_active():
            return "visitor_photo_fast_poll"
        return None

    async def async_read_once(self) -> bool:
        """정기 회차 하나를 실행한다. 겹치는 요청은 기다리지 않고 버린다."""
        return await self._async_read() == "ok"

    async def async_manual_read(self) -> None:
        """버튼·서비스로 30분을 기다리지 않고 한 번 읽는다.

        정기 일정(:00:30/:30:30)은 그대로 유지한다. 정기 회차와 같은 안전
        규칙(호출 중 건너뛰기, 겹침 방지, 값 검증)을 따르고, 읽지 못한 이유는
        HA 화면에 오류로 알려 준다.
        """
        now = time.monotonic()
        if (
            self._last_manual_monotonic is not None
            and now - self._last_manual_monotonic < METER_MANUAL_MIN_INTERVAL_SECONDS
        ):
            wait = int(METER_MANUAL_MIN_INTERVAL_SECONDS - (now - self._last_manual_monotonic)) + 1
            raise HomeAssistantError(f"Meter was read just now; try again in {wait} s")
        outcome = await self._async_read()
        if outcome == "busy":
            raise HomeAssistantError("A meter read is already in progress")
        if outcome in ("ok", "error", "ignored"):
            # 실제로 월패드를 관찰한 경우에만 재요청 간격을 건다. 호출 중이라
            # 건너뛴 경우는 월패드를 건드리지 않았으므로 바로 다시 누를 수 있다.
            self._last_manual_monotonic = time.monotonic()
        if outcome == "visitor_call_active":
            raise HomeAssistantError("Meter read skipped: a visitor call is in progress")
        if outcome == "visitor_photo_fast_poll":
            raise HomeAssistantError(
                "Meter read skipped: visitor photo is being fetched; try again shortly"
            )
        if outcome != "ok":
            raise HomeAssistantError(f"Meter read failed: {self.data.get('last_error')}")

    async def _async_read(self) -> str:
        if self._read_lock.locked():
            return "busy"
        async with self._read_lock:
            skip = self._skip_reason()
            if skip:
                self._publish(last_skip_reason=skip)
                return skip
            attempt = datetime.now(tz=UTC)
            try:
                output = await self._client.exec(
                    METER_HELPER, timeout=METER_COMMAND_TIMEOUT_SECONDS
                )
                tid, readings = parse_helper_output(output)
            except Exception as err:  # noqa: BLE001 - 다음 회차에 다시 시도
                _LOGGER.debug("Meter read failed: %s", err)
                self._publish(last_attempt=attempt, last_error=str(err) or type(err).__name__,
                              last_skip_reason=None)
                return "error"
            return "ok" if self._accept(readings, tid, attempt) else "ignored"

    def _is_suspect(self, readings: dict[str, float]) -> bool:
        if self._accepted_monotonic is None:
            return False
        hours = (time.monotonic() - self._accepted_monotonic) / 3600 + 1
        for key in METER_KEYS:
            previous = self.data.get(key)
            if previous is None:
                continue
            delta = readings[key] - previous
            if delta < 0 or delta > _MAX_PER_HOUR[key] * hours:
                return True
        return False

    def _accept(self, readings: dict[str, float], tid: str, attempt: datetime) -> bool:
        if self._is_suspect(readings):
            self._suspect_count += 1
            if self._suspect_count < METER_SUSPECT_CONFIRMATIONS:
                self._publish(
                    last_attempt=attempt,
                    last_error=(
                        "implausible reading ignored "
                        f"({self._suspect_count}/{METER_SUSPECT_CONFIRMATIONS}): "
                        + ", ".join(f"{k}={readings[k]}" for k in METER_KEYS)
                    ),
                    last_skip_reason=None,
                )
                return False
            _LOGGER.warning("Accepting meter reading after repeated confirmation: %s", readings)
        self._suspect_count = 0
        self._accepted_monotonic = time.monotonic()
        self._publish(
            **readings,
            last_success=attempt,
            last_attempt=attempt,
            last_error=None,
            last_skip_reason=None,
            reader_thread=tid,
        )
        return True

    def _publish(self, **changes) -> None:
        self.async_set_updated_data({**self.data, **changes})
