"""MITM 라우터용 공유 SSH client — 모든 coordinator가 이 하나를 재사용한다.

poller(엘리베이터, 전력 사용량 등)마다 SSH 연결을 따로 여는 대신 연결 하나를
계속 유지하며 돌려쓴다. 라우터는 리소스가 넉넉하지 않아서, 여러 개의 SSH
세션이 각자 유휴 상태로 떠 있는 걸 피하고 싶기 때문이다. 호출하는 쪽은
`exec()`만 await하면 되고, 재연결 처리는 여기서 한 곳에서 맡는다. asyncssh는
버전마다 "연결이 살아있는지" 확인하는 API가 달라서 미리 검사하는 방식은
자꾸 깨졌다 — 그래서 아예 검사하지 않고, 캐시된 연결을 그냥 써보다가 실제로
실패하면 그때 재연결한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import warnings

# asyncssh의 crypto/cipher.py는 레거시 cipher 지원 때문에 `cryptography`
# 패키지에서 ARC4/TripleDES 심볼을 가져오는데, 이 cipher들을 실제로 쓰든 안
# 쓰든(우리는 안 쓴다 — 최신 cipher로 협상한다) import 시점에 매번
# CryptographyDeprecationWarning이 뜬다. 무해한 경고지만 HA 로그에는 눈에
# 띄는 경고로 남는다. HA 인스턴스의 다른 무관한 경고까지 가리지 않도록 이
# import 구간에만 범위를 좁혀서 숨긴다.
#
# cryptography 패키지는 이 경고를 일부러 DeprecationWarning이 아니라
# UserWarning의 서브클래스로 만들어서 기본 필터에 안 걸리게 해놨다
# (그래서 `simplefilter("ignore", DeprecationWarning)`만으로는 안 잡힌다 —
# 2026-08-26 실사용 로그로 확인). CryptographyDeprecationWarning을 직접
# 잡아야 한다.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from cryptography.utils import CryptographyDeprecationWarning

    warnings.simplefilter("ignore", CryptographyDeprecationWarning)
    import asyncssh

_LOGGER = logging.getLogger(__name__)


class CommaxRouterClient:
    """공유 SSH 연결로 OpenWrt 라우터에서 셸 명령을 실행한다."""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._conn: asyncssh.SSHClientConnection | None = None
        # 한 SSH 채널에서 여러 원격 명령의 stdout이 섞이지 않게 직렬화한다.
        # 라우터 도우미도 공용 lock을 쓰지만 HA 쪽에서 먼저 막는 편이 효율적이다.
        self._lock = asyncio.Lock()

    async def _open_connection(self) -> asyncssh.SSHClientConnection:
        # 라우터 재설치 시 host key가 바뀌어도 연결되도록 known_hosts 검사는
        # 사용하지 않는다. 따라서 이 연결은 신뢰하는 관리 LAN에서만 사용한다.
        #
        # rekey_seconds: asyncssh 기본값(3600초=1시간)을 그대로 두면, 방문자
        # 호출 감시처럼 장기간 유지되는 스트림에서 매시간 rekey 타이밍에
        # "Broken pipe"로 끊기는 게 실측으로 확인됐다(2026-08-26, HA 로그
        # `coordinator_visitor_call.py:102`). 기존 자동 재연결 로직 덕분에
        # 1~2분 안에는 복구되지만, 그 짧은 창에서 실제 호출을 놓칠 위험이
        # 있어 이 연결에서는 rekey 주기를 하루로 늘려 사실상 발생하지 않게
        # 한다. 라우터가 재부팅되면 연결 자체가 다시 열리므로 안전하다.
        return await asyncssh.connect(
            self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            known_hosts=None,
            connect_timeout=10,
            rekey_seconds=86400,
        )

    async def exec(self, command: str, timeout: int = 10) -> str:
        """라우터에서 `command`를 실행하고 stdout을 문자열로 반환한다.

        실패 시 재연결 후 1회 재시도하므로, 연결이 한 번 끊기는 정도는
        호출자에게 드러나지 않는다.
        """
        async with self._lock:
            last_err: Exception | None = None
            for _attempt in range(2):
                try:
                    # 정상일 때는 기존 연결을 계속 쓰고, 실제 명령 실패가 난
                    # 경우에만 아래 except에서 연결을 버리고 한 번 재접속한다.
                    if self._conn is None:
                        self._conn = await self._open_connection()
                    async with self._conn.create_process(command) as process:
                        return await asyncio.wait_for(
                            process.stdout.read(), timeout=timeout
                        )
                except (OSError, asyncssh.Error, asyncio.TimeoutError) as err:
                    last_err = err
                    if self._conn is not None:
                        self._conn.close()
                    self._conn = None

            raise last_err if last_err else RuntimeError("exec failed")

    async def exec_bytes(self, command: str, timeout: int = 15) -> bytes:
        """라우터 명령의 stdout을 바이너리로 반환한다.

        방문자 JPEG처럼 텍스트 디코딩을 거치면 손상되는 데이터 전용이다.
        기존 엘리베이터 명령과 달리 비정상 종료 상태도 검사해, ADB pull
        실패 메시지를 빈 이미지로 오인하지 않게 한다.
        """
        async with self._lock:
            last_err: Exception | None = None
            for _attempt in range(2):
                try:
                    if self._conn is None:
                        self._conn = await self._open_connection()
                    process = await self._conn.create_process(command, encoding=None)
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout
                    )
                    if process.exit_status != 0:
                        detail = stderr.decode("utf-8", errors="replace")[:300]
                        raise RuntimeError(
                            f"remote command exited {process.exit_status}: {detail}"
                        )
                    return stdout
                except RuntimeError:
                    # 원격 도우미 자체의 확정적 실패는 같은 명령을 즉시 반복하지 않는다.
                    raise
                except (OSError, asyncssh.Error, asyncio.TimeoutError) as err:
                    last_err = err
                    if self._conn is not None:
                        self._conn.close()
                    self._conn = None

            raise last_err if last_err else RuntimeError("binary exec failed")

    async def stream_lines(
        self,
        command: str,
        line_handler: Callable[[str], Awaitable[None]],
    ) -> None:
        """원격 장기 실행 명령의 stdout을 줄 단위로 전달한다.

        호출 감시 전용 client 인스턴스에서만 사용한다. 연결이 끊기거나 원격
        명령이 끝나면 예외를 발생시키며, 재연결 지연은 상위 monitor가 담당한다.
        """
        async with self._lock:
            if self._conn is None:
                self._conn = await self._open_connection()
            process: asyncssh.SSHClientProcess | None = None
            try:
                process = await self._conn.create_process(command)
                async for line in process.stdout:
                    await line_handler(line.rstrip("\r\n"))
                if process.exit_status not in (None, 0):
                    raise RuntimeError(
                        f"remote stream exited {process.exit_status}: {command}"
                    )
            except (OSError, asyncssh.Error):
                if self._conn is not None:
                    self._conn.close()
                self._conn = None
                raise
            finally:
                if process is not None and not process.is_closing():
                    # Closing an SSH channel alone can leave a remote shell
                    # pipeline and its adb child alive. Ask the helper to exit
                    # first so its trap can reap adb, then close the channel.
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait_closed(), timeout=3)
                    except (OSError, asyncio.TimeoutError):
                        process.close()
                        await process.wait_closed()

    async def close(self) -> None:
        """설정 항목 unload 시 공유 SSH 연결을 닫는다."""
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None
