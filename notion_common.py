"""setup_notion.py와 check_notion.py가 공통으로 쓰는 노션 API 호출 코드."""

import sys
import time

import requests


def force_utf8_console():
    """터미널로 내보내는 글자를 UTF-8로 고정한다 (명세서 §14-4).

    윈도우 명령 프롬프트는 기본 인코딩이 cp949라, 이 프로그램들이 찍는
    이모지(🤖 ⚠️ ❌)에서 UnicodeEncodeError가 나며 봇이 켜지다 죽는다.
    맥에서는 재현되지 않으므로 확인이 아니라 **고정**으로 막는다.
    errors="replace"를 두는 이유는, 찍지 못하는 글자 하나 때문에
    프로그램이 멈추는 것보다 그 글자만 ?로 나오는 편이 낫기 때문이다.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                # 파이프로 넘겨받은 출력 등 다시 설정할 수 없는 경우가 있다.
                # 여기서 막히더라도 프로그램이 멈출 이유는 없다.
                pass

# 노션 API 버전 (공식 문서 기준 최신). 모든 요청에 이 값 하나만 사용한다.
NOTION_VERSION = "2026-03-11"
API_BASE = "https://api.notion.com/v1"

# 노션 API는 초당 약 3회 제한이 있으므로 호출 사이에 간격을 둔다.
REQUEST_INTERVAL = 0.35

# 429(요청 과다)를 만났을 때의 재시도 규칙.
# /export처럼 조회를 연속으로 하면 429가 한 번으로 끝나지 않는다. 전에는 재시도가
# 1회뿐이라 두 번째 429에서 그대로 실패했다. 간격을 점점 벌리되 상한을 두어
# 무한히 기다리지 않게 한다. 노션이 Retry-After를 주면 그 값을 우선한다.
RETRY_LIMIT = 5            # 처음 1회 + 재시도 5회 = 최대 6번 보낸다
RETRY_FIRST_WAIT = 1.0     # 1 → 2 → 4 → 8 → 16초
RETRY_MAX_WAIT = 20.0      # 한 번에 이보다 오래 기다리지 않는다
RETRY_TOTAL_WAIT = 60.0    # 다 합쳐 이 시간을 넘기면 포기한다


# 연결 자체가 안 될 때 쓰는 가짜 응답의 상태 코드. HTTP에는 없는 값이라
# 진짜 응답과 섞이지 않는다.
NETWORK_FAILED = 0


class NetworkFailure:
    """DNS·연결 실패를 응답처럼 감싼 것.

    requests가 던지는 예외를 그대로 올려보내면 사용자 화면에 영어 스택트레이스가
    뜬다. 부르는 쪽(bot.py·check_notion.py·setup_notion.py)은 모두 `res.ok`와
    `explain_error(res)`만 보므로, 같은 모양을 흉내 낸 것 하나를 돌려주면
    세 파일이 한꺼번에 한국어 안내를 받는다. 영어 원문은 `text`에만 담아 두고
    터미널 로그로만 내보낸다.
    """

    ok = False
    status_code = NETWORK_FAILED

    def __init__(self, error):
        self.text = f"{type(error).__name__}: {error}"

    def json(self):
        return {}


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def retry_after(res, attempt):
    """이번에 기다릴 시간(초). 노션이 알려 준 값이 더 길면 그것을 따른다."""
    backoff = min(RETRY_FIRST_WAIT * (2 ** attempt), RETRY_MAX_WAIT)
    try:
        told = float(res.headers.get("Retry-After", "0"))
    except (TypeError, ValueError):
        told = 0.0
    return max(backoff, told)


def guarded(send):
    """요청을 보내되, 인터넷이 끊겨 예외가 나면 NetworkFailure로 바꾼다.

    자동 재시도는 하지 않는다. 연결이 안 되는 상태는 몇 초 기다린다고
    풀리지 않으므로, 안내하고 끝내는 편이 낫다.
    """
    try:
        return send()
    except requests.exceptions.RequestException as e:
        # 영어 원문은 터미널에만 남긴다. 사용자 화면에는 explain_error()가
        # 만든 한국어 안내만 나간다.
        print(f"[개발용 오류 원문] {type(e).__name__}: {e}")
        return NetworkFailure(e)


def with_retry(send):
    """429를 만나면 간격을 벌려 가며 여러 번 다시 보낸다.

    `send`는 응답을 돌려주는 함수 하나다. 다 써도 429면 마지막 429 응답을
    그대로 돌려주므로, 부르는 쪽은 explain_error()로 한국어 안내를 만들면 된다.
    """
    res = guarded(send)
    waited = 0.0
    for attempt in range(RETRY_LIMIT):
        if res.status_code != 429:
            return res
        wait = retry_after(res, attempt)
        if waited + wait > RETRY_TOTAL_WAIT:
            break
        print(f"⏳ 요청이 너무 잦습니다. {wait:.0f}초 기다린 뒤 다시 시도합니다"
              f" ({attempt + 1}/{RETRY_LIMIT}번째).")
        time.sleep(wait)
        waited += wait
        res = guarded(send)

    if res.status_code == 429:
        print(f"⏳ {waited:.0f}초를 기다렸지만 계속 요청이 거절되어 포기했습니다.")
    return res


def notion_send(method, path, token, payload):
    """노션 API에 본문이 있는 요청. 429면 간격을 벌려 가며 여러 번 다시 시도한다."""
    def send():
        time.sleep(REQUEST_INTERVAL)
        return requests.request(
            method, API_BASE + path, headers=headers(token), json=payload, timeout=30
        )

    return with_retry(send)


def notion_post(path, token, payload):
    return notion_send("POST", path, token, payload)


def notion_patch(path, token, payload):
    """블록 이어붙이기(PATCH /blocks/{id}/children)에 쓴다."""
    return notion_send("PATCH", path, token, payload)


def notion_get(path, token):
    """조회 요청. /export는 이 함수를 연속으로 부르므로 재시도가 특히 중요하다."""
    def send():
        time.sleep(REQUEST_INTERVAL)
        return requests.get(API_BASE + path, headers=headers(token), timeout=30)

    return with_retry(send)


NETWORK_HELP = (
    "❌ 노션에 연결하지 못했습니다.\n"
    "   인터넷 연결을 확인해 주세요.\n"
    "   - Wi-Fi가 켜져 있는지, 다른 사이트는 열리는지 확인해 주세요.\n"
    "   - 회사·학교 네트워크라면 방화벽이 api.notion.com 접속을 막고 있을 수 있습니다.\n"
    "   - VPN을 쓰고 있다면 잠시 끄고 다시 시도해 주세요.\n"
    "   (영어로 된 오류 원문은 이 터미널 창 위쪽에 적혀 있습니다.)"
)


def explain_error(res):
    """실패한 응답을 한국어 안내문으로 바꾼다."""
    if res.status_code == NETWORK_FAILED:
        return NETWORK_HELP
    if res.status_code == 401:
        return (
            "❌ 노션 토큰이 잘못되었습니다 (401).\n"
            "   확인할 곳: .env 파일의 NOTION_TOKEN 값\n"
            "   - https://www.notion.so/my-integrations 에서 토큰을 다시 복사하세요.\n"
            "   - 'ntn_' 또는 'secret_' 으로 시작하는 값 전체를 따옴표 없이 붙여넣어야 합니다."
        )
    if res.status_code == 404:
        return (
            "❌ 노션이 해당 페이지(또는 표)를 찾지 못했습니다 (404).\n"
            "   원인 1) .env의 NOTION_DB_ID 값이 잘못되었을 수 있습니다.\n"
            "          → 노션 표 주소에서 32자리 ID를 다시 복사해 넣어주세요.\n"
            "   원인 2) 표에 통합(integration)을 연결하지 않았을 수 있습니다. (가장 흔함)\n"
            "          → 노션에서 그 표를 열고 우측 상단 ... → 연결(Connections) → 만들어 둔 통합을 선택하세요."
        )
    if res.status_code == 429:
        return (
            "❌ 노션이 요청이 너무 많다고 합니다 (429).\n"
            f"   간격을 벌려 가며 {RETRY_LIMIT}번 다시 시도했지만 계속 거절되었습니다.\n"
            "   1~2분쯤 기다린 뒤 다시 시도해 주세요."
        )
    return (
        f"❌ 노션 API 오류입니다 (HTTP {res.status_code}).\n"
        f"   노션이 보낸 원문: {res.text}\n"
        "   확인할 곳: .env의 NOTION_TOKEN, 그리고 선택한 페이지의 통합 연결 상태"
    )


def fetch_data_source(db_id, token):
    """database_id로 data source의 id와 속성 목록을 가져온다.

    성공하면 (data_source_id, properties, None),
    실패하면 (None, None, 한국어 안내문)을 돌려준다.
    """
    res = notion_get(f"/databases/{db_id}", token)
    if not res.ok:
        return None, None, explain_error(res)

    data_sources = res.json().get("data_sources", [])
    if not data_sources:
        return None, None, (
            "❌ 이 데이터베이스에 data source가 없습니다.\n"
            "   노션 표가 올바르게 만들어졌는지 확인해 주세요."
        )

    ds_id = data_sources[0]["id"]
    res = notion_get(f"/data_sources/{ds_id}", token)
    if not res.ok:
        return None, None, explain_error(res)

    return ds_id, res.json().get("properties", {}), None


def select_option_names(properties, name):
    """선택(select) 속성 하나의 옵션 이름을 노션이 준 순서 그대로 돌려준다."""
    prop = properties.get(name)
    if prop is None:
        return None
    return [o["name"] for o in prop.get(prop.get("type"), {}).get("options", [])]


# ============================================================
# 학습 기록 표의 속성 (명세서 §1)
# ============================================================
# /setup이 "고른 표가 학습 기록 표가 맞는가"를 판별하는 데 쓴다.
# 노션 검색 결과에는 추출본 표(§17)도 함께 나오므로, 이름만 보고는 가릴 수 없다.
RECORD_PROPERTIES = {
    "개념명": "title",
    "과목": "select",
    "단원": "select",
    "개념태그": "multi_select",
    "지식유형": "select",
    "오답유형": "multi_select",
    "교재구분": "select",
    "문제위치": "rich_text",
    "참고출처": "rich_text",
    "기록일": "created_time",
}


# ============================================================
# 추출본 데이터베이스 (명세서 §17)
# ============================================================
# /export 결과를 모아 두는 별도의 표다. 학습 기록 표(§1)와 절대 섞지 않는다.
# 통합 정리본이 기록 표에 들어가면 개념태그 누적 횟수가 부풀고 보드 뷰가 망가진다.
# bot.py(자동 생성) · setup_notion.py(배포용 생성) · check_notion.py(선택 검사)가
# 같은 이름과 같은 속성을 봐야 하므로 정의를 여기 한 곳에 둔다.
EXPORT_DB_TITLE = "CPA 추출본"
EXPORT_ENV_KEY = "NOTION_EXPORT_DB_ID"

# 명세서 §17-2 표 그대로. 순서도 명세 순서를 따른다.
EXPORT_PROPERTIES = {
    "제목": "title",
    "과목": "select",
    "범위": "rich_text",
    "포함건수": "number",
    "생성일": "created_time",
}


def build_export_properties():
    """§17-2의 속성 5개를 노션 API가 받는 모양으로 만든다."""
    return {
        "제목": {"title": {}},
        # 옵션은 비워 둔다. 저장할 때 노션이 자동으로 만든다 (학습 기록 표의 `단원`과 같다).
        "과목": {"select": {"options": []}},
        "범위": {"rich_text": {}},
        "포함건수": {"number": {}},
        "생성일": {"created_time": {}},
    }


def create_export_database(parent_page_id, token):
    """추출본 표를 만든다. (database_id, 오류안내문)."""
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": EXPORT_DB_TITLE}}],
        "initial_data_source": {"properties": build_export_properties()},
    }
    res = notion_post("/databases", token, payload)
    if not res.ok:
        return None, explain_error(res)
    return res.json()["id"], None


def save_env_value(env_path, key, value):
    """`.env`의 한 줄을 고쳐 쓴다. 그 줄이 없으면 맨 뒤에 붙인다.

    파일이 아예 없으면 새로 만든다. 다른 줄은 건드리지 않는다.
    """
    try:
        with open(env_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []

    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")

    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
