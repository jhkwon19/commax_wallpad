"""Commax Wallpad (MITM) 통합의 상수 모음."""

DOMAIN = "commax_wallpad"

CONF_ELEVATOR_INDEX = "elevator_index"

DEFAULT_PORT = 22
DEFAULT_USERNAME = "root"
DEFAULT_ELEVATOR_INDEX = 0

# 조회할 때마다 lan1의 flood/learning을 순간적으로 끄고 중복 IP 소켓으로
# 실제 월패드 트래픽과 경합하기 때문에(wallpad-elevator-query.sh 참고),
# carFloor가 도착하기 전에 SOAP 응답이 잘리는 일이 가끔 생긴다. 짧은 재시도
# 1회로 이런 순간적인 손실을 넘기고, 일회성 실패마다 로그 오류가 남지 않게 한다.
ELEVATOR_MAX_ATTEMPTS = 2  # 첫 실패는 짧게 1회만 재시도
ELEVATOR_RETRY_DELAY_SECONDS = 1
# 일시적인 조회 실패 몇 번 정도는 마지막 정상 층수/방향을 유지하고,
# 단발 실패마다 엔티티를 곧장 unavailable로 바꾸지 않는다.
ELEVATOR_FAILURES_BEFORE_UNAVAILABLE = 3

SERVICE_REFRESH_ELEVATOR = "refresh_elevator"
# Compatibility alias: one refresh only, never periodic polling.
SERVICE_FAST_POLL = "fast_poll"

# 방문자 메타데이터 조회는 USB ADB의 디렉터리 목록만 읽고, 새 파일이 생긴
# 경우에만 JPEG를 전송한다. 공동현관 처리 자체를 방해하지 않으면서 체감상
# 충분히 빠르게 갱신되도록 10초로 둔다.
VISITOR_SCAN_INTERVAL_SECONDS = 10
VISITOR_ARCHIVE_BATCH_SIZE = 20
VISITOR_CALL_FAST_SCAN_INTERVAL_SECONDS = 1
VISITOR_CALL_FAST_POLL_DURATION_SECONDS = 20

# carDirection 값, 실제 운행으로 확인됨:
#   0 = 정지
#   1 = 상승 (2026-08-10 확인, 1층→17층 운행: carFloor가 오르는 동안
#       상승 구간 내내 1로 유지됨)
#   2 = 하강 (2026-08-10 확인, 39층→17층 운행: carFloor가
#       28→26→24→22→20→17로 내려가는 하강 구간 내내 2로 유지되다가,
#       도착 후 몇 초간 2에 머무름 — 도어오픈 dwell로 추정 — 이후 0으로 정착)
DIRECTION_IDLE = "정지"
DIRECTION_UP = "상승"
DIRECTION_DOWN = "하강"
DIRECTION_UNKNOWN = "알수없음"

DIRECTION_MAP = {
    0: DIRECTION_IDLE,
    1: DIRECTION_UP,
    2: DIRECTION_DOWN,
}

# evStatus 값, com.commax.elevator 앱(Util.getMessage) 리버싱으로 확인됨
# (2026-08-26). 다만 실제 세대 호출 2회(왕복 실측 + 1초 간격 raw 백그라운드
# 캡처 포함)에서 도착 순간에도 evStatus는 계속 1로 고정이었다 — 이 설치의
# 엘리베이터 컨트롤러가 9(도착완료)를 실제로 보내지 않는 것으로 보인다.
# 앱은 여러 제조사 어댑터에 공용으로 배포되므로 값 하나하나가 이 건물에서도
# 쓰인다는 보장은 없다. 그래서 evStatus=9를 이벤트로 쓰는 기능은 뺐고, 이
# 표는 "값이 온다면 뭘 뜻하는지"에 대한 참고용으로만 남겨둔다.
# wallpad-project 저장소의 docs/04-discovered-endpoints.md(evStatus 값 매핑) 참고.
EV_STATUS_UNKNOWN = "알수없음"
EV_STATUS_NORMAL = 1
EV_STATUS_ARRIVAL = 9

EV_STATUS_MAP = {
    0: "관제통신이상",
    EV_STATUS_NORMAL: "정상",
    2: "점검중",
    3: "독립운전",
    4: "화재운행",
    5: "고장",
    6: "주차상태",
    7: "만원",
    8: "비관제",
    EV_STATUS_ARRIVAL: "도착완료",
}
