"""Commax Wallpad (MITM) 통합의 config flow."""

from __future__ import annotations

import logging
import warnings
from typing import Any

# client.py 참고 — asyncssh의 무해한 legacy-cipher import 경고를 숨긴다.
# config flow는 __init__.py와 독립적으로 로드될 수 있어 client.py보다 먼저
# 이 모듈이 asyncssh를 import할 수도 있으므로, 이 import 하나에만 범위를
# 한정한다.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import asyncssh

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_ELEVATOR_INDEX,
    DEFAULT_ELEVATOR_INDEX,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# 동/호수와 월패드 MAC은 라우터 도우미가 관리한다. HA 설정에는 관리망 SSH와
# getEvStatus 인덱스만 받아 네트워크 세부 구현이 UI에 노출되지 않게 한다.
DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_ELEVATOR_INDEX, default=DEFAULT_ELEVATOR_INDEX): int,
    }
)


async def _test_ssh_login(host: str, port: int, username: str, password: str) -> None:
    """실패 시 예외를 던진다 — 설정 과정에서 자격증명 검증에만 사용."""
    # 여기서는 로그인 가능 여부만 확인하고 실제 공유 연결은 setup_entry에서 연다.
    conn = await asyncssh.connect(
        host,
        port=port,
        username=username,
        password=password,
        known_hosts=None,
        connect_timeout=10,
    )
    conn.close()


class CommaxElevatorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Commax Wallpad (MITM)의 config flow를 처리한다."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # 같은 라우터/인덱스를 중복 등록해 불필요한 중복 폴링이 생기는 것을 막는다.
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}:{user_input[CONF_ELEVATOR_INDEX]}"
            )
            self._abort_if_unique_id_configured()

            try:
                await _test_ssh_login(
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except asyncssh.PermissionDenied:
                # 비밀번호 오류와 네트워크 오류를 나눠 HA UI에 번역된 메시지로 표시한다.
                errors["base"] = "invalid_auth"
            except (OSError, asyncssh.Error):
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"Commax Wallpad ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )
