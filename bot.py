"""bot.py - 텔레그램 기록 입력 봇.

/new 명령으로 10단계 입력 흐름을 진행하고, 마지막에 노션에 기록을 저장한다.
지식유형·오답유형·교재구분의 선택지는 명세서 §9에 따라 노션에서 읽어 온다.
"""

import asyncio
import html
import json
import os
import sys
import traceback
from datetime import datetime
from io import BytesIO
from uuid import uuid4

import yaml
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import content_format
from notion_common import (
    EXPORT_DB_TITLE,
    EXPORT_ENV_KEY,
    EXPORT_PROPERTIES,
    RECORD_PROPERTIES,
    create_export_database,
    explain_error,
    fetch_data_source,
    notion_get,
    force_utf8_console,
    notion_patch,
    notion_post,
    save_env_value,
    select_option_names,
)

# 윈도우 명령 프롬프트(cp949)에서 이모지를 찍다 죽지 않게 한다 (명세서 §14-4).
# import 시점에 고정해 두면 test_flow.py·add_test_pending.py도 함께 적용받는다.
force_utf8_console()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHAPTERS_PATH = os.path.join(BASE_DIR, "chapters.yaml")

# 단원 버튼은 한 화면에 10개씩 표시한다.
PAGE_SIZE = 10

# 노션 공식 문서(Request limits)에서 확인한 제한 수치.
#   - 한 요청에 넣을 수 있는 블록 배열의 길이: 100개
#     (페이지 생성의 children, PATCH /blocks/{id}/children 모두 같다)
# 100개로 자르는 것은 맨 바깥 블록만 센다. 목록의 children은 부모에 붙어 함께 가며,
# 그 배열도 각각 100개 제한을 받지만 정리 내용 한 항목에 자식이 100개 달릴 일은 없다.
# 글자 수 2000자 제한은 content_format.TEXT_CHUNK가 맡는다.
BLOCK_LIMIT = 100

# 텔레그램 Bot API sendDocument 제한 (공식 문서에서 확인: "Bots can currently send
# files of any type of up to 50 MB in size"). 여유를 두고 45MB에서 파일을 나눈다.
TELEGRAM_FILE_LIMIT = 45 * 1024 * 1024

# 저장에 실패한 기록을 담아 두는 로컬 백업 파일. .gitignore에 넣어 두었다.
PENDING_PATH = os.path.join(BASE_DIR, "pending_records.json")

# 노션에서 옵션을 읽어 오는 선택형 속성 (명세서 §9).
NOTION_OPTION_PROPERTIES = ["지식유형", "오답유형", "교재구분"]

# 입력 단계 순서. 인덱스가 곧 진행 위치다.
STEPS = [
    "subject",
    "category",
    "chapter",
    "tag",
    "knowledge",
    "error",
    "source",
    "problem",
    "basis",
    "content",
]

CHAPTER_STEP = STEPS.index("chapter")
TAG_STEP = STEPS.index("tag")
SOURCE_STEP = STEPS.index("source")
PROBLEM_STEP = STEPS.index("problem")

# 단계 이름 → 수집한 값을 담을 이름
FIELDS = {
    "subject": "과목",
    "category": "대분류",
    "chapter": "단원",
    "tag": "개념태그",
    "knowledge": "지식유형",
    "error": "오답유형",
    "source": "교재구분",
    "problem": "문제위치",
    "basis": "참고출처",
    "content": "정리 내용",
}

PROMPTS = {
    "subject": "과목을 선택해 주세요.",
    "category": "대분류를 선택해 주세요.",
    "chapter": "단원을 선택해 주세요.",
    "tag": "개념태그를 선택해 주세요.  (여러 개 고를 수 있습니다)",
    "knowledge": "지식유형을 선택해 주세요.",
    "error": "오답유형을 선택해 주세요.  (여러 개 고를 수 있습니다)",
    "source": "교재구분을 선택해 주세요.",
    "problem": (
        "문제 위치를 입력하세요 (예: 연p.212#15)\n"
        "※ 연=연습서 객=객관식 기=기출 모=모의고사\n"
        "※ 같은 문제끼리 묶으려면 표기를 통일하세요"
    ),
    # 문제위치는 "이 문제가 어디 있었나", 참고출처는 "이 개념의 근거가 어디 있나"다.
    # 두 칸의 차이가 드러나도록 예시의 성격을 서로 다르게 둔다 (명세서 §10-6).
    "basis": "참고 출처를 입력하세요 (예: 기준서 1116호 문단 22 / 워p.131)",
    "content": "정리 내용을 입력해 주세요.  (필수)",
}

# 건너뛰기가 가능한 단계
SKIPPABLE = {"error", "source", "problem", "basis"}

# 여러 개를 골라 [✔️ 선택 완료]로 넘어가는 단계 (명세서 §10 · 노션 multi_select)
MULTI_STEPS = {"tag", "error"}

# 텍스트로 직접 입력받는 단계
TEXT_STEPS = {"problem", "basis", "content"}

# [📌 같은 문제로 하나 더]에서 그대로 이어 쓰는 값 (명세서 §11-1)
KEEP_FIELDS = ["과목", "대분류", "단원", "교재구분", "문제위치"]

# [📌 같은 문제로 하나 더] 이후 다시 묻지 않고 지나갈 단계 (명세서 §11-1).
# 값이 있든 건너뛰었든 "이미 정해진 것"으로 보고 화면을 띄우지 않는다.
KEEP_STEPS = {"source", "problem"}

# 저장하려면 반드시 값이 있어야 하는 단계. build_properties()가 이 값들을
# .get() 없이 그대로 꺼내 쓰므로, 하나라도 비면 파이썬 오류가 난다.
REQUIRED_STEPS = ["subject", "category", "chapter", "tag", "knowledge", "content"]

# 봇이 시작할 때 한 번만 읽어 여기에 담아 둔다.
CHAPTERS = {}
OPTIONS = {}
NOTION_TOKEN = ""
DATA_SOURCE_ID = ""

# 학습 기록 표(§1). 추출본 표를 만들 때 이 표의 부모 페이지를 찾는 데 쓴다 (§17-3).
NOTION_DB_ID = ""
# 추출본 표(§17). 없으면 빈 문자열이고, 처음 [📄 노션에 저장]을 누를 때 만들어진다.
EXPORT_DB_ID = ""
# 추출본 표의 data source. 한 번 찾으면 봇이 켜져 있는 동안 다시 찾지 않는다.
EXPORT_DATA_SOURCE_ID = ""

ENV_PATH = os.path.join(BASE_DIR, ".env")


# ============================================================
# chapters.yaml 읽기와 검사
# ============================================================
def load_chapters():
    """chapters.yaml을 읽어 검사한다. 형식이 잘못됐으면 안내 후 프로그램을 끝낸다."""
    if not os.path.exists(CHAPTERS_PATH):
        die(f"chapters.yaml 파일을 찾을 수 없습니다.\n   찾은 위치: {CHAPTERS_PATH}")

    try:
        with open(CHAPTERS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        die(
            "chapters.yaml의 형식이 잘못되었습니다.\n"
            f"   원문 오류: {e}\n"
            "   들여쓰기는 공백 2칸 단위여야 하고, 탭 문자는 쓸 수 없습니다."
        )

    if not isinstance(data, dict) or not data:
        die("chapters.yaml에 과목이 하나도 없습니다.\n   '과목: / 대분류: / - 단원' 형식으로 채워 주세요.")

    for subject, categories in data.items():
        if not isinstance(categories, dict) or not categories:
            die(f"chapters.yaml의 '{subject}' 아래에 대분류가 없습니다.\n   과목 밑에는 대분류를 두어야 합니다.")
        for category, chapters in categories.items():
            if not isinstance(chapters, list) or not chapters:
                die(f"chapters.yaml의 '{subject} > {category}' 아래에 단원이 없습니다.\n   '- 단원이름' 형식으로 채워 주세요.")
            for chapter in chapters:
                if not isinstance(chapter, str):
                    die(f"chapters.yaml의 '{subject} > {category}'에 글자가 아닌 단원이 있습니다: {chapter!r}")

    return data


def die(message):
    print("[오류] " + message)
    sys.exit(1)


# ============================================================
# 노션에서 옵션 목록 읽기 (명세서 §9)
# ============================================================
def load_notion_options(db_id, token):
    """봇 시작 시 1회만 호출한다. 실패하면 안내 후 프로그램을 끝낸다.

    노션이 준 순서를 그대로 쓴다. 임의로 정렬하지 않는다.
    """
    ds_id, properties, error = fetch_data_source(db_id, token)
    if error:
        print(error)
        print("\n   노션에서 선택지를 읽지 못해 봇을 시작할 수 없습니다.")
        sys.exit(1)

    options = {}
    for name in NOTION_OPTION_PROPERTIES:
        names = select_option_names(properties, name)
        if names is None:
            die(
                f"노션 표에 `{name}` 속성이 없습니다.\n"
                "   속성 이름은 명세서와 글자 단위로 같아야 합니다.\n"
                "   → check_notion.py를 실행해 어떤 속성이 어긋났는지 확인해 주세요."
            )
        if not names:
            die(
                f"노션의 `{name}` 속성에 선택지가 하나도 없습니다.\n"
                "   선택지가 없으면 봇이 버튼을 만들 수 없습니다.\n"
                f"   → 노션에서 `{name}` 속성을 열고 선택지를 1개 이상 추가한 뒤 봇을 다시 켜 주세요."
            )
        options[name] = names

    return ds_id, options


# ============================================================
# 저장 실패 기록의 로컬 백업 (목록 형태의 JSON 파일 하나)
# ============================================================
def read_pending():
    """백업 파일을 읽는다. (항목 목록, 치워 둔 손상 파일 경로).

    파일이 손상되었으면 봇을 죽이지 않는다. 원본을 `.broken`으로 옮겨 두고
    빈 목록으로 시작한다. 옮겨 두는 이유는 손상된 내용도 사용자의 학습 기록이라
    말없이 덮어써 버리면 안 되기 때문이다.
    """
    if not os.path.exists(PENDING_PATH):
        return [], None

    try:
        with open(PENDING_PATH, encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            raise ValueError("목록(JSON 배열) 형태가 아닙니다.")
    except (OSError, ValueError):
        broken = PENDING_PATH + ".broken"
        try:
            os.replace(PENDING_PATH, broken)
        except OSError:
            broken = None
        return [], broken

    return [migrate_item(it) for it in items if isinstance(it, dict) and it.get("id")], None


# v1.5에서 속성 이름 3개가 바뀌었다. 그 전에 만들어진 백업 항목은 옛 이름을 갖고
# 있으므로, 파일에서 읽어 들일 때 새 이름으로 바꿔 준다. 옛 이름 그대로 보내면
# 노션이 "그런 속성이 없다"는 영어 오류를 돌려주고 복구가 통째로 막힌다.
RENAMED_PROPERTIES = {
    "출처구분": "교재구분",
    "문제출처": "문제위치",
    "근거": "참고출처",
}

# 노션에 보내려면 반드시 들어 있어야 하는 속성. build_properties()가 언제나
# 채우는 것들이며, 하나라도 비면 노션이 영어 오류를 돌려준다.
REQUIRED_PROPERTIES = ["개념명", "과목", "단원", "개념태그", "지식유형"]


def migrate_item(item):
    """백업 항목 하나의 옛 속성 이름을 새 이름으로 바꾼다 (명세서 §1, v1.5)."""
    properties = item.get("속성")
    if not isinstance(properties, dict):
        return item
    for old, new in RENAMED_PROPERTIES.items():
        if old in properties:
            properties.setdefault(new, properties[old])
            properties.pop(old)
    return item


def check_item(item):
    """백업 항목 하나가 노션에 보낼 수 있는 모양인지 본다. 문제가 없으면 None.

    do_save()는 세션 값으로 같은 검사를 하지만, 백업 파일에서 복구하는 경로는
    그 검사를 지나지 않는다. 손상된 파일이 들어오면 영어 오류가 그대로 새어
    나가므로 보내기 전에 여기서 한국어로 걸러 낸다.
    """
    properties = item.get("속성")
    if not isinstance(properties, dict):
        return "`속성`이 들어 있지 않습니다. 백업 파일이 손상된 것 같습니다."
    missing = [name for name in REQUIRED_PROPERTIES if not properties.get(name)]
    if missing:
        return "필수 속성이 비어 있습니다: " + ", ".join(missing)
    content = item.get("정리 내용")
    if not isinstance(content, str) or not content.strip():
        return "`정리 내용`이 비어 있습니다."
    return None


def write_pending(items):
    """백업 파일을 통째로 다시 쓴다. 쓰다가 죽어도 원본이 깨지지 않게 임시 파일을 거친다.

    백업에 실패하는 것은 저장 자체를 막을 이유가 되지 않으므로 안내만 하고 넘어간다.
    """
    tmp = PENDING_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PENDING_PATH)
    except OSError as e:
        print(f"[안내] 백업 파일에 쓰지 못했습니다: {e}")


def pending_put(item):
    """항목을 넣거나, 같은 id가 이미 있으면 덮어쓴다."""
    items, _ = read_pending()
    items = [it for it in items if it.get("id") != item["id"]]
    items.append(item)
    write_pending(items)


def pending_remove(item_id):
    items, _ = read_pending()
    remaining = [it for it in items if it.get("id") != item_id]
    if len(remaining) != len(items):
        write_pending(remaining)


def pending_label(item):
    """알림 한 줄에 쓸 표시. 예: `과소금액상각 · 연p.21-3#기01 (09/08 17:18)`"""
    try:
        when = datetime.fromisoformat(item["시각"]).strftime("%m/%d %H:%M")
    except (KeyError, TypeError, ValueError):
        when = "시각 모름"
    return f"{item.get('제목') or '(제목 없음)'} ({when})"


# ============================================================
# 노션 조회 / 저장
# ============================================================
def chapter_value(data):
    """노션 `단원` 속성에 넣을 `대분류-단원` 결합값 (명세서 §1)."""
    return f"{data['대분류']}-{data['단원']}"


def is_missing_option_error(res):
    """400 응답 중 '그 선택지가 없다'는 뜻인 것만 가려낸다.

    실측(2026-09-09)으로 확인한 400 발생 조건:
    **select 속성에 equals 필터를 걸 때, 그 값이 속성의 현재 옵션 목록에
    없으면 400이다.** 그 속성에 옵션이 몇 개 있는지는 상관이 없다.
    `단원` 옵션이 4개 있는 상태에서 없는 값으로 걸어도 400이 나왔고,
    옵션에 있는 값으로 걸면 해당 기록이 0건이어도 200에 빈 결과가 나왔다.
    (이전 주석의 "옵션이 하나라도 있으면 200"이라는 설명은 사실이 아니다.)

    옵션에 없는 값 = 그 값으로 저장한 기록이 0건이므로 조용히 넘어가도 되지만,
    속성 이름이 틀렸다거나 필터 타입이 어긋난 400은 사용자가 원문을 봐야 한다.

    영어 메시지 한 줄에만 기대면 노션이 문구를 바꾸는 순간 판정이 뒤집힌다.
    그래서 응답의 오류 코드(validation_error)도 함께 본다. 둘 다 맞아야
    "옵션이 없을 뿐"으로 보고 조용히 넘어간다. 코드를 읽지 못하는 응답이면
    예전처럼 메시지만 보고 판단한다.
    """
    if "not found for property" not in (res.text or ""):
        return False
    try:
        code = res.json().get("code")
    except ValueError:
        return True
    return code in (None, "validation_error")


def fetch_tags(chapter):
    """해당 단원의 기존 개념태그를 최근에 쓴 순으로 모으고 누적 횟수를 센다.

    성공하면 ([(태그, 횟수), ...], None), 실패하면 (None, 한국어 안내문).
    """
    counts = {}
    recent_first = []
    cursor = None

    while True:
        payload = {
            "filter": {"property": "단원", "select": {"equals": chapter}},
            "sorts": [{"property": "기록일", "direction": "descending"}],
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor

        res = notion_post(f"/data_sources/{DATA_SOURCE_ID}/query", NOTION_TOKEN, payload)
        if res.status_code == 400 and is_missing_option_error(res):
            # 이 단원으로 저장한 기록이 아직 없으면 `단원` 옵션 목록에 그 값이 없고,
            # 없는 값으로 거르면 노션이 400을 준다. 기록 0건과 같은 뜻이다.
            # 그 밖의 400은 진짜 오류이므로 아래에서 원문을 그대로 보여준다.
            return [], None
        if not res.ok:
            return None, explain_error(res)

        body = res.json()
        for page in body.get("results", []):
            selected = page.get("properties", {}).get("개념태그", {}).get("multi_select") or []
            for option in selected:
                name = option.get("name")
                if not name:
                    continue
                if name not in counts:
                    counts[name] = 0
                    recent_first.append(name)
                counts[name] += 1

        if not body.get("has_more"):
            break
        cursor = body.get("next_cursor")

    return [(name, counts[name]) for name in recent_first], None


def body_blocks(text):
    """정리 내용 원문을 노션 블록으로 바꾼다 (명세서 §13-1).

    변환 규칙은 content_format 한 곳에만 있다. /export의 복원도 같은 표를 쓰므로
    저장과 내보내기가 어긋날 수 없다 (명세서 §13-4).
    """
    return content_format.parse_content(text)


def build_title(data):
    """`대표태그 · 문제위치` — 문제위치가 없으면 `대표태그 · MM/DD` (명세서 §10-3).

    제목에는 대표 태그 하나만 쓴다. 둘 이상을 이으면 제목이 길어져 노션 표에서
    잘린다. v1.4까지 쓰던 "첫 번째로 선택한 태그" 규칙은 폐기되었다. 태그 목록이
    사용 빈도순이라 위에서부터 고르면 흔한 태그가 늘 제목이 되어, 같은 문제에서
    만든 기록들의 제목이 전부 같아지는 일이 실제로 있었다.
    """
    problem = data.get("문제위치")
    tail = problem if problem else datetime.now().strftime("%m/%d")
    return f"{star_of(data)} · {tail}"


def build_properties(data):
    """건너뛴 항목은 속성 자체를 넣지 않는다 (명세서 §8). 옵션값은 대조하지 않는다."""
    properties = {
        "개념명": {"title": [{"type": "text", "text": {"content": build_title(data)}}]},
        "과목": {"select": {"name": data["과목"]}},
        "단원": {"select": {"name": chapter_value(data)}},
        "개념태그": {"multi_select": [{"name": name} for name in data["개념태그"]]},
        "지식유형": {"select": {"name": data["지식유형"]}},
    }
    if data.get("오답유형"):
        properties["오답유형"] = {"multi_select": [{"name": name} for name in data["오답유형"]]}
    if data.get("교재구분"):
        properties["교재구분"] = {"select": {"name": data["교재구분"]}}
    for name in ("문제위치", "참고출처"):
        if data.get(name):
            properties[name] = {"rich_text": [{"type": "text", "text": {"content": data[name]}}]}

    return properties


def page_payload(properties, blocks):
    """페이지 생성 요청과, 한 요청에 못 담아 뒤로 미룬 블록 목록을 돌려준다.

    노션은 한 요청의 블록 배열을 BLOCK_LIMIT(100)개까지만 받으므로,
    앞 100개로 페이지를 만들고 나머지는 이어붙이기로 보낸다.
    """
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID},
        "properties": properties,
        "children": blocks[:BLOCK_LIMIT],
    }
    return payload, blocks[BLOCK_LIMIT:]


def send_record(item):
    """백업 항목 하나를 노션에 보낸다.

    (url, 오류안내문, 일부만저장됨, 알림목록) 을 돌려준다.
    - 성공          : (url, None, False, [])
    - 아예 실패     : (None, 안내문, False, [])
    - 일부만 저장됨 : (url, 안내문, True, [])  ← 페이지는 만들어졌으나 뒷부분 실패

    알림목록은 수식이 코드블록으로 바뀌었을 때의 한국어 안내다 (명세서 §13-3).

    이어붙이기는 notion_common이 호출마다 0.35초를 두므로 초당 약 3회 제한을
    넘지 않는다.
    """
    original = body_blocks(item.get("정리 내용") or "")

    # 명세서 §13-3 — 수식 실패 폴백.
    # 1단계: 빈 수식·2000자를 넘는 수식은 보내기 전에 코드블록으로 바꾼다.
    #        빈 수식은 노션에서 아무것도 없는 블록이 되어 내용이 사라진다.
    blocks, fallback = content_format.replace_equations(
        original, content_format.is_unusable_equation
    )
    payload, rest = page_payload(item["속성"], blocks)
    res = notion_post("/pages", NOTION_TOKEN, payload)

    # 2단계: 그래도 노션이 400으로 거부했고 수식이 들어 있다면, 수식 전부를
    #        코드블록으로 바꿔 한 번 더 보낸다. 수식 하나 때문에 기록 전체를
    #        잃지 않는 것이 목적이다. 번호는 언제나 원본 기준으로 센다.
    if res.status_code == 400:
        retried, every = content_format.replace_equations(original, lambda block: True)
        if every:
            fallback = every
            payload, rest = page_payload(item["속성"], retried)
            res = notion_post("/pages", NOTION_TOKEN, payload)

    notes = []
    notice = content_format.equation_notice(fallback)
    if notice:
        notes.append(notice)

    if not res.ok:
        return None, explain_error(res), False, []

    page = res.json()
    url = page.get("url")
    for start in range(0, len(rest), BLOCK_LIMIT):
        group = rest[start:start + BLOCK_LIMIT]
        res = notion_patch(f"/blocks/{page['id']}/children", NOTION_TOKEN, {"children": group})
        if not res.ok:
            return url, explain_error(res), True, notes

    return url, None, False, notes


def build_item(data, item_id):
    """저장 시도 직전에 로컬 백업에 남길 항목. 그대로 다시 보낼 수 있어야 한다."""
    return {
        "id": item_id or uuid4().hex,
        "시각": datetime.now().isoformat(timespec="seconds"),
        "제목": build_title(data),
        "속성": build_properties(data),
        "정리 내용": data.get("정리 내용") or "",
    }


def save_record(data, item_id=None):
    """노션에 기록 하나를 만든다. 보내기 직전에 로컬 백업에 남기고 성공하면 지운다.

    (url, 오류안내문, 일부만저장됨, 남은백업항목id, 알림목록) 을 돌려준다.
    [🔄 다시 시도]로 여러 번 눌러도 항목이 쌓이지 않도록 같은 id를 다시 쓴다.
    """
    item = build_item(data, item_id)
    pending_put(item)

    url, error, partial, notes = send_record(item)
    if error is None:
        pending_remove(item["id"])
        return url, None, False, None, notes

    # 실패하면 파일에 그대로 남긴다. [🔄 다시 시도] 버튼은 화면에 유지된다.
    return url, error, partial, item["id"], notes


# ============================================================
# 세션 (사용자가 지금까지 고른 값)
# ============================================================
def new_session():
    return {
        "step": 0,
        "data": {},
        "page": 0,
        "msg_id": None,
        "tag_mode": None,    # 개념태그의 하위 화면: input · search · star · unpick
        "tag_query": None,   # 검색어. [➕ 새 태그]를 거쳐 돌아와도 유지된다 (명세서 §12-3)
        "tags": None,        # [(태그, 횟수)] - 단원의 전체 목록
        "tag_counts": {},    # 태그 이름 → 이 단원에서의 기존 누적 횟수
        "notice": None,      # 화면 위에 한 번만 보여줄 안내문
        "again": False,      # [📌 같은 문제로 하나 더]로 이어진 흐름인가
        "locked": set(),     # 다시 묻지 않고 지나갈 단계 이름 (명세서 §11-1)
        "resume": None,      # [✏️ 출처 바꾸기]를 누른 자리. 출처를 고친 뒤 돌아온다
        "pending_id": None,  # 저장에 실패해 백업 파일에 남아 있는 항목의 id
        "saved": False,
        "damage": None,      # 손상 정황 안내문. 강행/취소를 고르기 전까지만 담긴다 (명세서 §15-2)
    }


def picked(session, field):
    """multi_select 단계에서 지금까지 고른 값 목록. 건너뛴 경우엔 빈 목록."""
    return session["data"].get(field) or []


def toggle(session, field, name):
    """고른 값을 넣거나 뺀다. 넣은 순서를 그대로 유지한다 (개념명 조합에 쓰인다)."""
    current = list(picked(session, field))
    if name in current:
        current.remove(name)
    else:
        current.append(name)
    session["data"][field] = current


def options_of(session):
    """현재 단계에서 버튼으로 보여줄 선택지 목록."""
    step = STEPS[session["step"]]
    data = session["data"]
    if step == "subject":
        return list(CHAPTERS.keys())
    if step == "category":
        return list(CHAPTERS[data["과목"]].keys())
    if step == "chapter":
        return CHAPTERS[data["과목"]][data["대분류"]]
    if step in ("knowledge", "error", "source"):
        return OPTIONS[FIELDS[step]]
    return []


def clear_from(session, index):
    """index 단계부터 뒤쪽에 모아 둔 값을 비운다.

    유지 중인 단계(교재구분·문제위치)는 건드리지 않는다. 다시 묻지 않는 값을
    비워 버리면 저장할 때 그 속성이 빠져 「같은 문제로 하나 더」가 무의미해진다.
    """
    for later in STEPS[index:]:
        if later in session["locked"]:
            continue
        session["data"].pop(FIELDS[later], None)


def reset_tag_screen(session):
    session["tag_mode"] = None
    session["tag_query"] = None
    session["tags"] = None
    # tag_counts는 여기서 지우지 않는다. 저장 화면의 누적 알림이 이 값을 쓰기 때문이다.
    # 태그 단계에 들어설 때마다 노션에서 다시 읽어 통째로 덮어쓴다.


def tag_candidates(session):
    """버튼으로 띄울 태그 (명세서 §12-1).

    이미 고른 태그는 본문 텍스트로 보여 주므로 버튼에서 뺀다. 선택분까지 버튼으로
    두면 다중 선택을 할수록 화면이 계속 길어진다. 검색어가 있으면 함께 걸러낸다.
    """
    chosen = picked(session, "개념태그")
    query = session["tag_query"]
    return [
        (name, count)
        for name, count in (session["tags"] or [])
        if name not in chosen and (not query or query in name)
    ]


def star_of(data):
    """대표 개념태그 (명세서 §12-2). 제목에 쓰는 태그 하나다.

    지정된 값이 선택 목록에 없으면 처음 고른 태그로 본다. 기본값이 "처음 선택한
    태그"이므로 대부분은 지정 자체가 없고, 뒤로 갔다 오거나 그 태그를 빼서 지정이
    낡았을 때도 이 되돌림이 알아서 맞춰 준다.
    """
    tags = data.get("개념태그") or []
    star = data.get("대표태그")
    if star in tags:
        return star
    return tags[0] if tags else None


def clamp_page(session, count):
    """항목이 줄어 지금 쪽 번호가 끝을 넘어가면 마지막 쪽으로 당긴다.

    태그를 고르면 그 태그가 버튼 목록에서 빠지므로 쪽 수가 줄어들 수 있다.
    쪽 번호를 0으로 되돌리지 않는 이유는, 뒤쪽 쪽에서 여러 개를 이어 고를 때
    매번 첫 쪽으로 튕기면 쓸 수 없기 때문이다.
    """
    total = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    if session["page"] >= total:
        session["page"] = total - 1


def split_tags(text):
    """쉼표로 나눈 새 태그 목록. 앞뒤 공백·빈 항목·중복을 없앤다.

    노션 선택지 이름에는 쉼표를 쓸 수 없으므로(명세서 §10-1),
    쉼표는 오직 태그를 나누는 구분자로만 쓴다.
    """
    names = []
    for piece in text.split(","):
        name = piece.strip()
        if name and name not in names:
            names.append(name)
    return names


# ============================================================
# 화면 만들기
# ============================================================
def build_path(session):
    """화면 상단에 보여줄 현재 선택 경로."""
    data = session["data"]
    parts = [data[name] for name in ("과목", "대분류", "단원") if data.get(name)]
    tags = picked(session, "개념태그")
    # 태그 단계에서는 선택분을 바로 아래 "선택:" 줄에 따로 보여 주므로 여기서 뺀다.
    on_tag_step = session["step"] < len(STEPS) and STEPS[session["step"]] == "tag"
    if tags and not on_tag_step:
        parts.append(" ".join(f"#{name}" for name in tags))
    return "📍 " + (" › ".join(parts) if parts else "(아직 선택 전)")


def build_kept_line(session):
    """[📌 같은 문제로 하나 더]로 이어받아 유지 중인 값 (명세서 §11-1).

    보관해 둔 문구를 다시 쓰지 않고 지금 세션 값으로 매번 만든다.
    그래야 [✏️ 출처 바꾸기]로 출처를 고친 뒤에도 화면이 어긋나지 않는다.
    """
    data = session["data"]
    place = [data["과목"]] if data.get("과목") else []
    if data.get("대분류") and data.get("단원"):
        place.append(chapter_value(data))
    elif data.get("대분류"):
        place.append(data["대분류"])

    parts = [" › ".join(place)] if place else []
    source = " ".join(v for v in (data.get("교재구분"), data.get("문제위치")) if v)
    if source:
        parts.append(source)
    elif "source" in session["locked"]:
        # 첫 기록에서 출처를 건너뛴 경우다. 건너뜀도 유지 대상이다.
        parts.append("출처 건너뜀")

    return "📌 유지 중: " + " · ".join(parts)


TAG_PROMPTS = {
    "input": "새 개념태그를 입력해 주세요.  (쉼표로 여러 개를 한 번에 넣을 수 있습니다)",
    "search": "찾을 글자를 입력해 주세요.  (태그의 일부만 적어도 됩니다)",
    "star": "제목에 쓸 대표 개념태그를 골라 주세요.",
    "unpick": "선택에서 뺄 개념태그를 골라 주세요.",
}


def build_tag_lines(session):
    """고른 태그를 본문 텍스트로 보여 주는 줄 (명세서 §12-1). 대표는 ⭐로 드러낸다."""
    tags = picked(session, "개념태그")
    if not tags:
        return ["선택: (아직 없음)"]

    star = star_of(session["data"])
    lines = ["선택: " + " · ".join(("⭐" + name if name == star else name) for name in tags)]
    if len(tags) > 1:
        lines.append("      ↑ 별표가 제목이 됩니다")
    return lines


def build_text(session):
    step = STEPS[session["step"]]
    mode = session["tag_mode"] if step == "tag" else None
    prompt = TAG_PROMPTS[mode] if mode else PROMPTS[step]

    lines = [build_path(session)]
    if session["again"]:
        lines.append(build_kept_line(session))

    # 경고는 맨 위에 놓는다. 아래쪽에 붙이면 태그 목록과 버튼에 밀려
    # [✔️ 선택 완료]를 빈 상태로 눌렀을 때의 안내를 못 보고 다시 누르게 된다.
    warning = session["notice"] if (session["notice"] or "").startswith("⚠️") else None
    if warning:
        lines.append("")
        lines.append(warning)

    lines.append("")
    lines.append(f"[{session['step'] + 1}/{len(STEPS)}] {prompt}")

    if step == "tag":
        lines += build_tag_lines(session)
        if session["tag_query"] and mode is None:
            lines.append(f"🔍 '{session['tag_query']}' 로 걸러 보는 중입니다")
    elif step in MULTI_STEPS:
        lines.append(f"선택됨: {len(picked(session, FIELDS[step]))}개")

    if session["notice"] and not warning:
        lines.append("")
        lines.append(session["notice"])
    return "\n".join(lines)


def toggle_label(name, chosen):
    """고른 값 앞에는 ✅를 붙인다."""
    return ("✅ " if chosen else "") + name


def page_slice(session, items):
    """PAGE_SIZE개씩 나눈 지금 쪽. ((원래인덱스, 항목) 목록, 전체 쪽수).

    단원 화면의 페이지 넘김 규격을 그대로 쓴다 (명세서 §12-1 · §7).
    원래 인덱스를 함께 돌려주므로 쪽을 넘겨도 버튼의 뜻이 달라지지 않는다.
    """
    if len(items) <= PAGE_SIZE:
        return list(enumerate(items)), 1
    total_pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
    start = session["page"] * PAGE_SIZE
    return list(enumerate(items))[start:start + PAGE_SIZE], total_pages


def pager_row():
    return [
        InlineKeyboardButton("◀ 이전", callback_data="pg:-1"),
        InlineKeyboardButton("PAGE", callback_data="noop"),
        InlineKeyboardButton("다음 ▶", callback_data="pg:1"),
    ]


def build_tag_rows(session):
    """개념태그 버튼 줄. 아직 고르지 않은 태그만 띄운다 (명세서 §12-1).

    옆 숫자는 이 단원에서의 누적 횟수다. 10개를 넘으면 쪽을 나눈다.
    """
    shown, total_pages = page_slice(session, tag_candidates(session))
    buttons = [
        InlineKeyboardButton(f"#{name} ({count})", callback_data=f"tag:{i}")
        for i, (name, count) in shown
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]

    if total_pages > 1:
        row = pager_row()
        row[1] = InlineKeyboardButton(f"{session['page'] + 1}/{total_pages}", callback_data="noop")
        rows.append(row)

    find = []
    if session["tags"]:
        find.append(InlineKeyboardButton("🔍 검색", callback_data="tagsearch"))
    if session["tag_query"]:
        find.append(InlineKeyboardButton("🔎 검색 해제", callback_data="tagclear"))
    find.append(InlineKeyboardButton("➕ 새 태그", callback_data="newtag"))
    rows.append(find)

    chosen = picked(session, "개념태그")
    manage = []
    if chosen:
        manage.append(InlineKeyboardButton("🗑 태그 빼기", callback_data="tagunpick"))
    if len(chosen) > 1:
        # 태그가 하나뿐이면 그것이 곧 대표이므로 버튼을 내보내지 않는다 (명세서 §12-2).
        manage.append(InlineKeyboardButton("⭐ 대표 바꾸기", callback_data="tagstar"))
    if manage:
        rows.append(manage)
    return rows


def build_chosen_rows(session, prefix):
    """이미 고른 태그만 버튼으로 띄운다. 대표 지정과 선택 빼기가 같은 모양을 쓴다."""
    star = star_of(session["data"])
    buttons = [
        InlineKeyboardButton(("⭐" if name == star else "") + f"#{name}", callback_data=f"{prefix}:{i}")
        for i, name in enumerate(picked(session, "개념태그"))
    ]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def build_keyboard(session):
    step = STEPS[session["step"]]
    rows = []

    if step == "tag":
        mode = session["tag_mode"]
        if mode is None:
            rows += build_tag_rows(session)
        elif mode in ("star", "unpick"):
            rows += build_chosen_rows(session, mode)
        # input·search는 글자를 기다리는 화면이라 선택 버튼을 두지 않는다.
    else:
        items = options_of(session)
        columns = 1 if step in ("category", "chapter") else 2

        if len(items) > PAGE_SIZE and step in ("chapter", "knowledge", "error", "source"):
            shown, total_pages = page_slice(session, items)
            rows += chunk_buttons(session, step, shown, columns)
            row = pager_row()
            row[1] = InlineKeyboardButton(f"{session['page'] + 1}/{total_pages}", callback_data="noop")
            rows.append(row)
        else:
            rows += chunk_buttons(session, step, list(enumerate(items)), columns)

    busy = step == "tag" and session["tag_mode"] is not None
    if step in MULTI_STEPS and not busy:
        done = InlineKeyboardButton("✔️ 선택 완료", callback_data="multidone")
        if step in SKIPPABLE:
            rows.append([InlineKeyboardButton("⏭ 건너뛰기", callback_data="skip"), done])
        else:
            rows.append([done])
    elif step in SKIPPABLE:
        rows.append([InlineKeyboardButton("⏭ 건너뛰기", callback_data="skip")])

    # 출처를 유지 중이고 개념태그 단계까지 온 뒤에만 보여 준다 (명세서 §11-1).
    # 그보다 앞 단계에서는 아직 묻지 않은 단계가 남아 있어 출처로 건너뛸 수 없다.
    # 태그의 하위 화면(새 태그 입력·검색·대표·빼기)에서는 감춘다. 잘못 누르면
    # 입력하던 흐름이 끊기고, 그 화면에서 할 일과도 상관이 없다.
    if session["locked"] and session["step"] >= TAG_STEP and not busy:
        rows.append([InlineKeyboardButton("✏️ 출처 바꾸기", callback_data="editsource")])

    rows.append([
        InlineKeyboardButton("⬅️ 뒤로", callback_data="back"),
        InlineKeyboardButton("❌ 처음부터", callback_data="reset"),
    ])
    return InlineKeyboardMarkup(rows)


def chunk_buttons(session, step, indexed_items, columns):
    """(원래인덱스, 이름) 목록을 columns개씩 줄로 나눈다."""
    chosen = picked(session, FIELDS[step]) if step in MULTI_STEPS else []
    buttons = [
        InlineKeyboardButton(toggle_label(name, name in chosen), callback_data=f"pick:{i}")
        for i, name in indexed_items
    ]
    return [buttons[i:i + columns] for i in range(0, len(buttons), columns)]


def build_saved_text(session):
    """누적 2회 이상인 태그만 최대 3개까지 알린다 (명세서 §10-4)."""
    lines = ["✅ 저장 완료"]
    counts = session["tag_counts"]
    repeated = [
        f"#{name} {counts.get(name, 0) + 1}번째"
        for name in picked(session, "개념태그")
        if counts.get(name, 0) + 1 >= 2
    ]
    if repeated:
        lines.append("⚠️ " + " · ".join(repeated[:3]))
    return "\n".join(lines)


def build_saved_keyboard(url):
    rows = []
    if url:
        rows.append([InlineKeyboardButton("🔗 노션에서 보기", url=url)])
    rows.append([
        InlineKeyboardButton("📌 같은 문제로 하나 더", callback_data="again"),
        InlineKeyboardButton("🏁 종료", callback_data="done"),
    ])
    return InlineKeyboardMarkup(rows)


def build_failed_text(what, reason):
    return (
        f"❌ 노션 {what}에 실패했습니다.\n"
        f"{reason}\n\n"
        "입력하신 내용은 그대로 남아 있습니다. [🔄 다시 시도]를 눌러 주세요."
    )


def build_partial_text(reason):
    """페이지는 만들어졌지만 뒷부분을 이어붙이다 실패한 경우. 성공처럼 알리지 않는다."""
    return (
        "⚠️ 일부만 저장되었습니다.\n"
        "정리 내용이 길어 여러 번에 나눠 보내는 중에 실패했습니다.\n"
        f"{reason}\n\n"
        "아래 링크의 노션 페이지에는 앞부분만 들어가 있습니다.\n"
        "입력하신 내용 전체는 로컬 백업에 그대로 남겨 두었습니다.\n"
        "[🔄 다시 시도]를 누르면 새 페이지로 처음부터 다시 저장합니다.\n"
        "중복을 피하려면 먼저 노션에서 위 페이지를 지운 뒤 눌러 주세요."
    )


def build_partial_keyboard(url):
    rows = []
    if url:
        rows.append([InlineKeyboardButton("🔗 노션에서 보기", url=url)])
    rows.append([InlineKeyboardButton("🔄 다시 시도", callback_data="retry")])
    rows.append([InlineKeyboardButton("❌ 처음부터", callback_data="reset")])
    return InlineKeyboardMarkup(rows)


FAILED_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔄 다시 시도", callback_data="retry")],
    [InlineKeyboardButton("❌ 처음부터", callback_data="reset")],
])


# ============================================================
# 화면 보내기 / 갱신
# ============================================================
async def send(session, context, chat_id, text, keyboard, query=None):
    """버튼을 눌렀을 때는 기존 메시지를 편집하고, 텍스트를 입력받았을 때는 새 화면을 아래에 다시 띄운다."""
    if query is not None:
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        return

    if session["msg_id"]:
        # 옛 화면 지우기는 대화창을 깔끔하게 유지하기 위한 보조 동작일 뿐이다.
        # 실패해도 본래 보내려던 메시지를 막지 않는다. 예전에는 BadRequest만
        # 넘겨서, 순간적인 네트워크 끊김(NetworkError)이 나면 그 뒤의
        # "저장 완료" 표시까지 통째로 중단됐다.
        # 사용자에게는 알리지 않는다. 옛 메시지가 하나 더 남는 정도의 일이다.
        try:
            await context.bot.delete_message(chat_id, session["msg_id"])
        except Exception as e:
            print(f"[안내] 옛 화면을 지우지 못했습니다(대화창에 그대로 남습니다): {e}")
    sent = await context.bot.send_message(chat_id, text, reply_markup=keyboard)
    session["msg_id"] = sent.message_id


# 저장 완료 화면처럼 놓치면 곤란한 전송에만 쓰는 재시도 (명세서 밖 · 사고 대응).
# 텔레그램 통신에는 라이브러리 차원의 재시도가 없다. python-telegram-bot 22.5의
# HTTPXRequest에는 재시도 옵션 자체가 없고, 그 아래 httpx의 연결 재시도도 기본이 0이다.
# 모든 전송에 일괄로 붙이지 않고 이 경로에서만 쓴다.
SAVED_SEND_TRIES = 3       # 처음 1회 + 재시도 2회
SAVED_SEND_FIRST_WAIT = 1.0  # 1 → 2초


async def send_saved(session, context, chat_id, text, keyboard, query=None):
    """저장 완료 화면을 보낸다. 순간적인 끊김이면 잠깐 기다렸다 다시 시도한다.

    여기서 다시 보내도 노션에 중복 저장될 일은 없다. 노션 저장은 이미 끝났고
    이 함수는 화면만 다룬다.
    """
    for attempt in range(SAVED_SEND_TRIES):
        try:
            await send(session, context, chat_id, text, keyboard, query)
            return
        except (NetworkError, TimedOut) as e:
            if attempt == SAVED_SEND_TRIES - 1:
                raise
            wait = SAVED_SEND_FIRST_WAIT * (2 ** attempt)
            print(
                f"[안내] 저장 완료 화면을 보내지 못했습니다. {wait:.0f}초 뒤 다시 시도합니다"
                f" ({attempt + 1}/{SAVED_SEND_TRIES - 1}번째): {e}"
            )
            await asyncio.sleep(wait)


def build_saved_unshown_text(url):
    """노션 저장은 끝났는데 화면 표시만 실패한 경우의 전용 안내.

    일반 오류 안내와 반드시 구분해야 한다. 일반 안내처럼 "다시 시도"로 읽히면
    사용자가 같은 내용을 한 번 더 보내 노션에 중복 저장된다.
    """
    lines = [
        "✅ 노션에는 정상적으로 저장되었습니다.",
        "화면 표시에만 실패했습니다.",
        "",
        "⚠️ 다시 보내지 마세요. 같은 내용이 두 번 저장됩니다.",
        "노션에서 확인해 보시고, /new 로 다음 기록을 시작하세요.",
    ]
    if url:
        lines += ["", f"저장된 페이지: {url}"]
    return "\n".join(lines)


async def report_saved_unshown(context, chat_id, url, error):
    """전용 안내를 보내고, 그 전송조차 실패하면 터미널에 남긴다.

    사용자가 나중에 터미널 창을 보고 "저장은 됐구나"를 알 수 있어야 한다.
    """
    print("[안내] 노션 저장은 성공했지만 저장 완료 화면을 보내지 못했습니다.")
    print(f"   원인: {error}")
    if url:
        print(f"   저장된 노션 페이지: {url}")
    print("   같은 내용을 다시 보내지 마십시오. 노션에 두 번 저장됩니다.")

    try:
        await context.bot.send_message(chat_id, build_saved_unshown_text(url))
    except Exception as e:
        print(f"[안내] 위 내용을 텔레그램으로도 보내지 못했습니다: {e}")
        print("   텔레그램 화면에는 아무 안내도 뜨지 않았습니다. 위 내용을 참고하십시오.")


async def show(session, context, chat_id, query=None):
    """현재 단계 화면을 띄운다. 개념태그 단계에 들어설 때마다 노션에서 태그를 다시 읽는다."""
    if STEPS[session["step"]] == "tag" and session["tags"] is None:
        tags, error = await asyncio.to_thread(fetch_tags, chapter_value(session["data"]))
        if error:
            await send(session, context, chat_id, build_failed_text("조회", error), FAILED_KEYBOARD, query)
            return
        session["tags"] = tags
        session["tag_counts"] = dict(tags)

    await send(session, context, chat_id, build_text(session), build_keyboard(session), query)
    session["notice"] = None


def first_missing_step(session):
    """저장에 필요한 값 중 아직 비어 있는 첫 단계의 인덱스. 다 있으면 None."""
    for name in REQUIRED_STEPS:
        if not session["data"].get(FIELDS[name]):
            return STEPS.index(name)
    return None


async def do_save(session, context, chat_id, query=None):
    """노션에 저장하고 결과 화면을 띄운다. 실패해도 세션을 지우지 않는다."""
    # 안전망. 어떤 경로로든 단계를 건너뛰어 값이 비었다면 파이썬 오류를 내는 대신
    # 그 값을 입력하는 단계로 되돌린다.
    missing = first_missing_step(session)
    if missing is not None:
        session["step"] = missing
        session["page"] = 0
        session["resume"] = None
        reset_tag_screen(session)
        session["notice"] = (
            f"⚠️ `{FIELDS[STEPS[missing]]}` 값이 비어 있어 저장할 수 없습니다.\n"
            "   이 단계부터 다시 입력해 주세요. 나머지 입력값은 그대로 남아 있습니다."
        )
        await show(session, context, chat_id, query)
        return

    url, error, partial, pending_id, notes = await asyncio.to_thread(
        save_record, session["data"], session["pending_id"]
    )
    session["pending_id"] = pending_id

    if partial:
        session["saved"] = False
        await send(session, context, chat_id, build_partial_text(error), build_partial_keyboard(url), query)
        return

    if error:
        session["saved"] = False
        await send(session, context, chat_id, build_failed_text("저장", error), FAILED_KEYBOARD, query)
        return

    session["saved"] = True
    text = build_saved_text(session)
    if notes:
        # 수식이 코드블록으로 바뀐 안내 (명세서 §13-3). 저장 자체는 성공이다.
        text += "\n" + "\n".join(notes)

    # 여기부터는 노션 저장이 이미 끝난 뒤다. 화면을 못 띄웠다고 해서 일반 오류로
    # 다루면 사용자가 다시 시도해 노션에 중복 저장된다. 전용 안내로 갈라낸다.
    try:
        await send_saved(session, context, chat_id, text, build_saved_keyboard(url), query)
    except Exception as e:
        await report_saved_unshown(context, chat_id, url, e)
        # 세션을 정리해 [뒤로] 같은 버튼이 어중간한 상태로 남지 않게 한다.
        # 남겨 두면 옛 화면의 버튼이 아무 반응도 하지 않는 것처럼 보인다.
        context.user_data.pop("session", None)


def advance(session):
    """다음 단계로 넘어간다. 마지막 단계까지 끝났으면 True를 돌려준다.

    이 함수가 이번 수정의 핵심이다. 전에는 step을 1씩만 올렸기 때문에
    「같은 문제로 하나 더」로 값을 유지해 놓아도 교재구분·문제위치 화면이
    그대로 다시 떴다.
    """
    session["step"] += 1

    # [✏️ 출처 바꾸기]를 누른 자리로 되돌린다. 문제위치를 지나야 되돌아간다.
    # 고쳐 놓은 출처를 이 기록의 나머지 단계에서 다시 묻지 않도록 잠금도 되살린다.
    if session["resume"] is not None and session["step"] > PROBLEM_STEP:
        session["step"] = session["resume"]
        session["resume"] = None
        if session["again"]:
            session["locked"] = set(KEEP_STEPS)

    # 유지 중인 단계는 묻지 않고 지나간다 (명세서 §11-1).
    while session["step"] < len(STEPS) and STEPS[session["step"]] in session["locked"]:
        session["step"] += 1

    session["page"] = 0
    reset_tag_screen(session)
    return session["step"] >= len(STEPS)


def step_back(session):
    """한 단계 뒤로. 유지 중인 단계는 건너뛴다 (축약된 흐름에서도 올바른 이전 단계로).

    [✏️ 출처 바꾸기]로 기억해 둔 자리(resume)도 함께 지운다. 남겨 두면 출처를
    고치다 뒤로 간 뒤에도 그 자리로 되돌아가려 해서, 이미 채운 단계를 저장 직전에
    한 번 더 묻게 된다.
    """
    session["step"] -= 1
    while session["step"] > 0 and STEPS[session["step"]] in session["locked"]:
        session["step"] -= 1
    session["resume"] = None


def restart_same_problem(session):
    """[📌 같은 문제로 하나 더]: 지정된 값만 남기고 개념태그 단계로 되돌린다.

    교재구분·문제위치는 값이 None(건너뜀)이어도 키를 남긴다. "건너뜀"도
    이미 정해진 값이므로 유지 대상이며, KEEP_STEPS로 잠가 다시 묻지 않는다.
    """
    session["data"] = {name: session["data"].get(name) for name in KEEP_FIELDS}
    session["step"] = TAG_STEP
    session["page"] = 0
    session["notice"] = None
    session["saved"] = False
    session["pending_id"] = None
    session["again"] = True
    session["locked"] = set(KEEP_STEPS)
    session["resume"] = None
    session["damage"] = None
    # 개념태그·오답유형은 KEEP_FIELDS에 없으므로 위에서 이미 지워졌다.
    # 화면에 남은 태그 목록도 비워 다음 기록에 선택이 따라붙지 않게 한다.
    reset_tag_screen(session)


def release_keep(session):
    """「같은 문제로 하나 더」의 유지를 풀고 일반 흐름으로 되돌린다.

    단원보다 앞 단계까지 뒤로 가면 "같은 문제" 전제 자체가 깨진다.
    유지 배너와 [✏️ 출처 바꾸기]를 감추고, 유지하던 출처도 해제해
    출처 단계가 다시 등장하게 한다.
    """
    session["again"] = False
    session["locked"] = set()
    session["resume"] = None
    session["data"].pop("교재구분", None)
    session["data"].pop("문제위치", None)


def edit_source(session):
    """[✏️ 출처 바꾸기]: 유지를 풀고 교재구분 선택 화면으로 되돌린다.

    **누른 자리를 언제나 기억해 둔다.** 예전에는 출처 단계보다 뒤에서 눌렀을
    때만 기억했기 때문에, 앞쪽(개념태그·지식유형·오답유형)에서 누르면 그
    단계들을 묻지 않고 건너뛴 채 저장까지 진행되어 오류가 났다.
    """
    session["locked"] = set()
    session["data"].pop("교재구분", None)
    session["data"].pop("문제위치", None)
    session["resume"] = session["step"]
    session["step"] = SOURCE_STEP
    session["page"] = 0
    session["notice"] = None
    reset_tag_screen(session)


# ============================================================
# 손상 감지 안전망 (명세서 §15-2)
# ============================================================
DAMAGE_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("🔁 그래도 저장", callback_data="force"),
    InlineKeyboardButton("❌ 취소", callback_data="damagecancel"),
]])


def build_damage_text(reason):
    """모바일에서 보내 서식 기호가 소실된 정황일 때의 안내 (명세서 §15-2).

    조용히 반쪽만 저장되는 것이 최악이므로, 저장하기 전에 되돌려 보낸다.
    복구는 불가능하다. 다시 보내는 것 말고는 방법이 없다 (명세서 §14-1).
    """
    return (
        "⚠️ 서식이 깨진 것 같습니다.\n"
        f"   판정: {reason}\n"
        "컴퓨터 텔레그램에서 다시 보내주세요.\n\n"
        "휴대폰에서 보내면 `$$` `##` `- ` 같은 기호가 사라진 채 도착합니다.\n"
        "지금 보낸 내용 그대로 저장하려면 [🔁 그래도 저장]을 눌러 주세요."
    )


# ============================================================
# 재시작 알림 (백업 파일에 남은 기록)
# ============================================================
PENDING_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("🔄 지금 저장", callback_data="pnd:save"),
    InlineKeyboardButton("🗑 버리기", callback_data="pnd:drop"),
]])

PENDING_CONFIRM_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("🗑 네, 버립니다", callback_data="pnd:dropyes"),
    InlineKeyboardButton("↩️ 아니요", callback_data="pnd:dropno"),
]])


def build_pending_text(items):
    lines = [f"📌 저장하지 못한 기록이 {len(items)}건 있습니다."]
    for i, item in enumerate(items, start=1):
        lines.append(f"   {i}) {pending_label(item)}")
    return "\n".join(lines)


def retry_pending():
    """남은 항목을 순서대로 저장한다. 성공한 것만 파일에서 지우고 결과를 알린다.

    **한 항목이 실패해도 나머지 항목의 복구는 계속한다.** 손상된 항목은 보내기 전에
    걸러 내고, 보내다 터진 항목은 여기서 받아 한국어로 알린다. 실패한 것은 파일에
    그대로 남겨 두므로 원인을 고친 뒤 다시 시도할 수 있다.
    """
    items, _ = read_pending()
    if not items:
        return "📌 저장하지 못한 기록이 이제 없습니다."

    lines = ["📌 저장 결과"]
    left = 0
    for i, item in enumerate(items, start=1):
        title = item.get("제목") or "(제목 없음)"

        broken = check_item(item)
        if broken:
            left += 1
            lines.append(f"   {i}) {title} — ❌ 백업 내용이 온전하지 않아 보내지 않았습니다")
            lines.append(f"      {broken}")
            continue

        try:
            url, error, partial, notes = send_record(item)
        except Exception as e:
            # 여기서 막지 않으면 한 건의 오류가 나머지 복구까지 통째로 멈춘다.
            # 영어 원문은 터미널에만 남기고 사용자에게는 한국어로 알린다.
            left += 1
            print(f"[안내] 백업 항목 '{title}'을(를) 보내다 오류가 났습니다.")
            traceback.print_exception(type(e), e, e.__traceback__)
            lines.append(f"   {i}) {title} — ❌ 보내는 중 오류가 났습니다 (백업에 그대로 남김)")
            lines.append("      오류 원문은 봇을 켜 둔 터미널 창에 적혀 있습니다.")
            continue

        if error is None:
            pending_remove(item["id"])
            lines.append(f"   {i}) {title} — ✅ 저장했습니다")
            for note in notes:
                lines.append(f"      {note}")
        elif partial:
            left += 1
            lines.append(f"   {i}) {title} — ⚠️ 일부만 저장되었습니다 (백업에 그대로 남김)")
        else:
            left += 1
            lines.append(f"   {i}) {title} — ❌ 실패했습니다")
        if error:
            lines.append(f"      {error.splitlines()[0]}")
        if url:
            lines.append(f"      {url}")

    if left:
        lines.append("")
        lines.append(f"{left}건은 백업 파일에 그대로 남겨 두었습니다. 원인을 고친 뒤 다시 시도해 주세요.")
    return "\n".join(lines)


async def notify_pending(app):
    """봇이 켜질 때 남은 항목을 알린다. 알림이 실패해도 기동은 그대로 진행한다.

    저장할 표가 아직 정해지지 않았으면 그것부터 알린다 (명세서 §7).
    """
    if not NOTION_DB_ID:
        try:
            await app.bot.send_message(ALLOWED_USER_ID, NOT_READY_TEXT)
        except Exception as e:
            print(f"[안내] /setup 안내를 텔레그램으로 보내지 못했습니다: {e}")

    items, broken = read_pending()

    if broken:
        print("[안내] 백업 파일이 손상되어 읽을 수 없었습니다.")
        print(f"   원본은 여기로 옮겨 두었습니다: {broken}")
        print("   봇은 그대로 정상 실행됩니다.")

    if not items:
        return

    try:
        await app.bot.send_message(ALLOWED_USER_ID, build_pending_text(items), reply_markup=PENDING_KEYBOARD)
    except Exception as e:
        # 알림 실패는 기동을 막을 이유가 되지 않는다. 콘솔에만 남긴다.
        print(f"[안내] 저장하지 못한 기록 {len(items)}건의 알림을 텔레그램으로 보내지 못했습니다: {e}")
        print("   기록은 백업 파일에 그대로 있습니다.")


async def on_pending_button(update, context, action):
    """재시작 알림의 버튼. 입력 흐름(session)과 무관하게 동작한다."""
    query = update.callback_query

    if action == "pnd:save":
        await query.edit_message_text("⏳ 저장하는 중입니다...")
        text = await asyncio.to_thread(retry_pending)
        await query.edit_message_text(text)
        return

    if action == "pnd:drop":
        items, _ = read_pending()
        if not items:
            await query.edit_message_text("📌 저장하지 못한 기록이 이제 없습니다.")
            return
        await query.edit_message_text(
            f"🗑 저장하지 못한 기록 {len(items)}건을 모두 버릴까요?\n"
            "버리면 되돌릴 수 없습니다.\n\n"
            + build_pending_text(items),
            reply_markup=PENDING_CONFIRM_KEYBOARD,
        )
        return

    if action == "pnd:dropyes":
        write_pending([])
        await query.edit_message_text("🗑 저장하지 못한 기록을 모두 버렸습니다.")
        return

    if action == "pnd:dropno":
        items, _ = read_pending()
        if not items:
            await query.edit_message_text("📌 저장하지 못한 기록이 이제 없습니다.")
            return
        await query.edit_message_text(build_pending_text(items), reply_markup=PENDING_KEYBOARD)
        return


# ============================================================
# 발신자 제한
# ============================================================
ALLOWED_USER_ID = None


def is_allowed(update):
    user = update.effective_user
    if user is None:
        return False
    if user.id == ALLOWED_USER_ID:
        return True
    print(f"[경고] 허용되지 않은 사용자 접근 시도 - id={user.id}, 이름={user.full_name}")
    return False


# ============================================================
# /raw 진단 명령 (명세서 §14-1 실측용 — 파서 아님, 측정만 한다)
# ============================================================
# 역슬래시와 긴 대시가 목록에 있어야 §15-3이 남겨 둔 두 물음에 답할 수 있다.
#   - iOS가 `\`를 지우는가 (지우면 `\frac`이 `frac`이 되어 감지가 새는 원인이었다)
#   - iOS가 하이픈을 긴 대시(—)로 바꾸는가
RAW_MARKERS = ["$$", ":::", "## ", "- ", "_", "`", "\\", "\u2014"]
RAW_MARKER_LABELS = {"`": "` (백틱)", "\\": "\\ (역슬래시)", "\u2014": "\u2014 (긴 대시)"}
TG_CHUNK_LIMIT = 3500


def chunk_html_escaped(text):
    """html.escape한 텍스트를 텔레그램 길이 제한 안에서 조각낸다.

    &amp; 같은 이스케이프 시퀀스 중간에서 자르면 <pre> 렌더가 깨지므로
    미완성 시퀀스가 있으면 그 앞에서 자른다.
    """
    escaped = html.escape(text)
    if not escaped:
        return [""]
    chunks = []
    while len(escaped) > TG_CHUNK_LIMIT:
        cut = TG_CHUNK_LIMIT
        amp_idx = escaped.rfind("&", 0, cut)
        if amp_idx != -1 and ";" not in escaped[amp_idx:cut]:
            cut = amp_idx
        chunks.append(escaped[:cut])
        escaped = escaped[cut:]
    chunks.append(escaped)
    return chunks


async def send_pre_blocks(context, chat_id, label, text):
    chunks = chunk_html_escaped(text)
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        header = f"{label} ({i}/{total})" if total > 1 else label
        await context.bot.send_message(chat_id, f"{header}\n<pre>{chunk}</pre>", parse_mode="HTML")


def format_entities(entities):
    if not entities:
        return "(entities 없음)"
    lines = []
    for i, ent in enumerate(entities):
        parts = [f"type={ent.type}", f"offset={ent.offset}", f"length={ent.length}"]
        if ent.url:
            parts.append(f"url={ent.url}")
        if ent.language:
            parts.append(f"language={ent.language}")
        if ent.custom_emoji_id:
            parts.append(f"custom_emoji_id={ent.custom_emoji_id}")
        if ent.user:
            parts.append(f"user={ent.user.id}")
        lines.append(f"[{i}] " + ", ".join(parts))
    return "\n".join(lines)


def format_marker_summary(text):
    lines = []
    for marker in RAW_MARKERS:
        label = RAW_MARKER_LABELS.get(marker, marker)
        lines.append(f"{label} : {text.count(marker)}개")
    # 이 글을 정리 내용으로 보냈다면 손상 감지에 걸리는지 그대로 보여 준다.
    # 실측과 판정을 한 화면에서 보려는 것이다 (명세서 §15-3).
    reason = content_format.detect_corruption(text)
    lines.append("")
    lines.append(f"손상 감지 : {reason if reason else '걸리지 않음 (정상 원문으로 판정)'}")
    return "\n".join(lines)


async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if context.user_data.get("session") is not None or context.user_data.get("export") is not None:
        await update.message.reply_text(
            "⚠️ /new 또는 /export 진행 중에는 /raw를 사용할 수 없습니다.\n"
            "진행 중인 흐름을 끝내거나 취소한 뒤 다시 시도해 주세요."
        )
        return
    context.user_data["raw_wait"] = True
    await update.message.reply_text(
        "🔍 /raw — 서식이 깨질 때 원인을 확인하는 진단 도구입니다.\n"
        "기록을 남기는 명령이 아닙니다. 보낸 글이 노션에 저장되지 않습니다.\n\n"
        "다음에 보내는 메시지를 그대로 뜯어 보여 드립니다."
    )


async def do_raw_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("raw_wait", None)
    message = update.message
    text = message.text or ""
    chat_id = update.effective_chat.id

    entities_text = format_entities(message.entities)
    summary_text = format_marker_summary(text)

    print("=" * 60)
    print("[안내] /raw 진단 결과")
    print("[원문]")
    print(text)
    print("-" * 60)
    print("[entities]")
    print(entities_text)
    print("-" * 60)
    print("[특수문자 요약]")
    print(summary_text)
    print("=" * 60)

    await send_pre_blocks(context, chat_id, "📄 원문 (message.text)", text)
    await send_pre_blocks(context, chat_id, "🏷 entities", entities_text)
    await send_pre_blocks(context, chat_id, "🔢 특수문자 요약", summary_text)
    await context.bot.send_message(
        chat_id, "ℹ️ 이 화면은 문제 진단용입니다. 기록은 /new 로 시작하세요."
    )


# ============================================================
# /setup 표 고르기 (명세서 §7 "대상 DB 지정 (2단 방식)")
# ============================================================
# 배포받은 사람이 노션 표 주소에서 32자리 ID를 눈으로 잘라내는 단계를 없애는 것이
# 목적이다. 조회와 .env 기록만 하며 **노션에 아무것도 쓰지 않는다.**
#
# 노션 API 2026-03-11의 검색은 `page`와 `data_source`만 받는다(`database`는 400).
# 그래서 data_source를 검색하고, 그 parent에 적힌 database_id를 .env에 적는다.
# 검색 결과에는 추출본 표(§17)도 함께 나오므로 이름만으로는 가릴 수 없다.
# 고른 뒤 속성 검사로 잘못 고른 경우를 잡는다.

MANUAL_ID_GUIDE = (
    "표 주소에서 ID를 복사해 .env에 넣어주세요.\n\n"
    "1) 노션에서 학습 기록 표를 엽니다.\n"
    "2) 우측 상단 ··· → '링크 복사'를 누릅니다.\n"
    "3) 복사한 주소는 이런 모양입니다.\n"
    "   https://www.notion.so/내작업공간/8f2c1d4e5a6b7c8d9e0f1a2b3c4d5e6f?v=...\n"
    "                          └── 이 32자리가 ID입니다 ──┘\n"
    "   · 물음표(?) 앞의 마지막 토막입니다.\n"
    "   · 주소에 제목이 붙어 있으면(예: CPA-지식-OS-8f2c...) 마지막 하이픈 뒤입니다.\n"
    "   · 하이픈이 섞여 있어도 그대로 붙여넣으면 됩니다.\n"
    "4) .env 파일을 열어 아래처럼 적고 저장합니다.\n"
    "   NOTION_DB_ID=8f2c1d4e5a6b7c8d9e0f1a2b3c4d5e6f\n"
    "5) 봇을 껐다가(Ctrl+C) 다시 켭니다."
)

NOT_READY_TEXT = (
    "⚠️ 아직 어느 노션 표에 저장할지 정해지지 않았습니다.\n"
    "/setup 으로 표를 선택해 주세요."
)


def search_record_databases():
    """통합이 접근할 수 있는 표 목록을 가져온다. (항목 목록, 오류안내문).

    항목 하나는 {제목, db_id, ds_id, properties} 이다. 검색 응답이 속성까지
    같이 주므로, 고른 뒤에 표를 다시 조회할 필요가 없다.
    """
    items = []
    cursor = None
    while True:
        payload = {"filter": {"property": "object", "value": "data_source"}, "page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = notion_post("/search", NOTION_TOKEN, payload)
        if not res.ok:
            return None, explain_error(res)
        data = res.json()
        for one in data.get("results", []):
            db_id = one.get("parent", {}).get("database_id")
            if not db_id:
                continue
            title = "".join(piece.get("plain_text", "") for piece in one.get("title", []))
            items.append({
                "제목": title or "(제목 없음)",
                "db_id": db_id,
                "ds_id": one.get("id"),
                "properties": one.get("properties", {}),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return items, None


def describe_table_problem(title, properties):
    """고른 표가 학습 기록 표(§1)가 맞는지 본다. 맞으면 None.

    추출본 표(§17)를 잘못 고르는 경우가 가장 흔하므로 따로 짚어 준다.
    """
    names = set(properties)

    if names == set(EXPORT_PROPERTIES) or (
        "포함건수" in names and "개념태그" not in names
    ):
        return (
            f"❌ `{title}` 은(는) /export 가 만든 **추출본 표**입니다.\n"
            "   이 표는 내보낸 정리본을 모아 두는 곳이라 기록을 저장할 수 없습니다.\n"
            "   (추출본 표는 봇이 알아서 만들고 관리하니 손대지 않으셔도 됩니다.)\n\n"
            "   → 학습 기록 표를 다시 골라 주세요. /setup 으로 다시 시작할 수 있습니다."
        )

    missing = []
    wrong = []
    for name, expected in RECORD_PROPERTIES.items():
        actual = properties.get(name)
        if actual is None:
            missing.append(name)
        elif actual.get("type") != expected:
            wrong.append(f"`{name}` — 있어야 할 타입 {expected} / 지금 {actual.get('type')}")

    if not missing and not wrong:
        return None

    lines = [f"❌ `{title}` 은(는) 학습 기록 표의 규격과 맞지 않습니다."]
    if missing:
        lines.append("   없는 속성: " + ", ".join(f"`{n}`" for n in missing))
    if wrong:
        lines.append("   타입이 다른 속성:")
        lines += [f"     · {one}" for one in wrong]
    lines.append("")
    lines.append("   → 표를 잘못 고르셨을 수 있습니다. /setup 으로 다시 골라 보세요.")
    lines.append("   → 이 표가 맞다면 check_notion.py 를 실행해 어디가 어긋났는지 보세요.")
    return "\n".join(lines)


def apply_chosen_table(item):
    """고른 표를 봇이 쓰도록 붙인다. (성공했는가, 안내문).

    노션에는 쓰지 않는다. .env의 NOTION_DB_ID 한 줄만 기록한다.
    """
    global NOTION_DB_ID, DATA_SOURCE_ID, OPTIONS

    problem = describe_table_problem(item["제목"], item["properties"])
    if problem:
        return False, problem

    # 속성이 §1과 맞으므로 이 표가 학습 기록 표인 것은 확정이다. 선택지가 비어 있어
    # 아직 못 쓰더라도 ID는 적어 둔다. 다시 고르게 만들 이유가 없다.
    save_env_value(ENV_PATH, "NOTION_DB_ID", item["db_id"])

    empty = []
    options = {}
    for name in NOTION_OPTION_PROPERTIES:
        names = select_option_names(item["properties"], name)
        if not names:
            empty.append(name)
        options[name] = names or []

    if empty:
        return False, (
            f"✅ `{item['제목']}` 을(를) .env에 적어 두었습니다.\n\n"
            "⚠️ 다만 아래 속성에 선택지가 하나도 없어 아직 버튼을 만들 수 없습니다.\n"
            + "\n".join(f"   · `{name}`" for name in empty)
            + "\n\n   → 노션에서 그 속성을 열고 선택지를 1개 이상 추가한 뒤,\n"
            "      이 창에서 Ctrl+C 로 봇을 끄고 다시 켜 주세요."
        )

    NOTION_DB_ID = item["db_id"]
    DATA_SOURCE_ID = item["ds_id"]
    OPTIONS = options

    counts = " / ".join(f"{name} {len(OPTIONS[name])}개" for name in NOTION_OPTION_PROPERTIES)
    return True, (
        f"✅ `{item['제목']}` 표를 쓰기로 했습니다.\n"
        "   속성 10개가 명세와 맞는 것을 확인했습니다.\n"
        f"   선택지를 읽었습니다 — {counts}\n\n"
        f"   .env의 NOTION_DB_ID에 적어 두었으니 다음부터는 /setup 없이 켜집니다.\n\n"
        "이제 /new 로 기록을 시작하실 수 있습니다."
    )


def build_setup_text(setup):
    lines = ["⚙️ 저장할 노션 표 고르기"]
    if NOTION_DB_ID:
        lines.append(f"   지금 쓰는 표의 ID: {NOTION_DB_ID}")
        lines.append("   다른 표로 바꾸려면 아래에서 고르세요.")
    lines.append("")
    lines.append(f"접근할 수 있는 표 {len(setup['items'])}개를 찾았습니다.")
    lines.append("학습 기록을 저장할 표를 골라 주세요.")
    lines.append("")
    lines.append("※ 고른 뒤에 규격이 맞는지 검사합니다. 잘못 골라도 기록이 망가지지 않습니다.")
    return "\n".join(lines)


def build_setup_keyboard(setup):
    items = setup["items"]
    clamp_page(setup, len(items))
    # 단원 화면과 같은 페이지 넘김 규격을 쓴다 (명세서 §7).
    shown, total_pages = page_slice(setup, items)
    rows = [[InlineKeyboardButton(one["제목"], callback_data=f"st:pick:{i}")] for i, one in shown]
    if total_pages > 1:
        rows.append([
            InlineKeyboardButton("◀ 이전", callback_data="st:pg:-1"),
            InlineKeyboardButton(f"{setup['page'] + 1}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton("다음 ▶", callback_data="st:pg:1"),
        ])
    rows.append([InlineKeyboardButton("❌ 취소", callback_data="st:cancel")])
    return InlineKeyboardMarkup(rows)


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    # 진행 중인 흐름과 섞이지 않게 한다. 안내만 하고 아무것도 하지 않는다.
    if context.user_data.get("session") is not None:
        await update.message.reply_text(
            "⚠️ /new 기록을 입력하는 중에는 /setup 을 쓸 수 없습니다.\n"
            "진행 중인 입력을 끝내거나 [❌ 처음부터]로 취소한 뒤 다시 시도해 주세요."
        )
        return
    if context.user_data.get("export") is not None:
        await update.message.reply_text(
            "⚠️ /export 를 진행하는 중입니다.\n"
            "그 화면의 [❌ 취소]를 누른 뒤 /setup 으로 다시 시도해 주세요."
        )
        return

    context.user_data.pop("raw_wait", None)
    await update.message.reply_text("🔎 노션에서 접근할 수 있는 표를 찾는 중입니다...")

    items, error = await asyncio.to_thread(search_record_databases)

    if error:
        await update.message.reply_text(
            f"{error}\n\n표를 찾지 못했습니다. 직접 넣으셔도 됩니다.\n\n{MANUAL_ID_GUIDE}"
        )
        return
    if not items:
        await update.message.reply_text(
            "📭 통합이 접근할 수 있는 표가 하나도 없습니다.\n"
            "   노션에서 표를 열고 우측 상단 ··· → 연결(Connections) 에서\n"
            "   만들어 둔 통합을 추가했는지 확인해 주세요.\n\n"
            f"{MANUAL_ID_GUIDE}"
        )
        return

    setup = {"items": items, "page": 0}
    context.user_data["setup"] = setup
    await context.bot.send_message(
        update.effective_chat.id, build_setup_text(setup), reply_markup=build_setup_keyboard(setup)
    )


async def on_setup_button(update, context, action):
    """/setup 화면의 버튼. /new·/export와 다른 상태를 쓴다."""
    query = update.callback_query
    setup = context.user_data.get("setup")
    if setup is None:
        await query.edit_message_text("끝난 화면입니다. /setup 으로 다시 시작해 주세요.")
        return

    if action == "st:cancel":
        context.user_data.pop("setup", None)
        await query.edit_message_text("❌ 표 고르기를 취소했습니다. /setup 으로 다시 시작할 수 있습니다.")
        return

    if action.startswith("st:pg:"):
        total_pages = max(1, (len(setup["items"]) + PAGE_SIZE - 1) // PAGE_SIZE)
        setup["page"] = (setup["page"] + int(action[6:])) % total_pages
        await query.edit_message_text(
            build_setup_text(setup), reply_markup=build_setup_keyboard(setup)
        )
        return

    if action.startswith("st:pick:"):
        index = int(action[8:])
        if index >= len(setup["items"]):
            return
        ok, message = apply_chosen_table(setup["items"][index])
        if ok:
            context.user_data.pop("setup", None)
            await query.edit_message_text(message)
            return
        # 잘못 골랐거나 아직 못 쓰는 표다. 목록은 그대로 두어 다시 고를 수 있게 한다.
        await query.edit_message_text(
            message + "\n\n다시 고르려면 /setup 을 보내 주세요.",
        )
        context.user_data.pop("setup", None)
        return


# ============================================================
# /export 마크다운 내보내기 (명세서 §16)
# ============================================================
# 시험 직전에 원하는 범위의 기록을 한 파일로 모아 읽기 위한 기능이다.
# 본문 복원은 저장 파서의 역방향이며 content_format의 같은 대응표를 쓴다
# (명세서 §13-4 · §16-4). 여기에 별도의 변환표를 만들면 안 된다.

EXPORT_SCOPES = [
    ("all", "📚 과목 전체"),
    ("chapter", "📂 단원으로 좁히기"),
    ("tag", "🏷 개념태그로 좁히기"),
]

# 파일 이름에 쓸 수 없는 글자. 공백은 밑줄로 바꾼다.
FILENAME_BAD = '/\\:*?"<>|'

EXPORT_KEEP_PROPERTIES = ["과목", "단원", "개념태그", "지식유형", "오답유형"]


def new_export():
    # records — 모아 둔 기록의 마크다운. 받는 방법을 고르는 동안 여기 남아 있어야
    # [📥 파일로 받기]와 [📄 노션에 저장]이 같은 것을 쓴다 (명세서 §16-5).
    return {"stage": "subject", "data": {}, "page": 0, "items": [],
            "msg_id": None, "records": []}


def property_value(page, name):
    """노션이 돌려준 페이지에서 속성 하나를 사람이 읽는 글자로 꺼낸다."""
    prop = page.get("properties", {}).get(name)
    if not prop:
        return ""
    kind = prop.get("type")
    if kind == "title":
        return "".join(piece.get("plain_text", "") for piece in prop.get("title") or [])
    if kind == "rich_text":
        return "".join(piece.get("plain_text", "") for piece in prop.get("rich_text") or [])
    if kind == "select":
        return (prop.get("select") or {}).get("name") or ""
    if kind == "multi_select":
        return " ".join(option.get("name", "") for option in prop.get("multi_select") or [])
    if kind == "created_time":
        return (prop.get("created_time") or "")[:10]
    return ""


def export_filter(data):
    """고른 범위를 노션 query의 filter로 바꾼다."""
    conditions = [{"property": "과목", "select": {"equals": data["과목"]}}]
    if data.get("단원"):
        conditions.append({"property": "단원", "select": {"equals": data["단원"]}})
    if data.get("개념태그"):
        conditions.append({"property": "개념태그", "multi_select": {"contains": data["개념태그"]}})
    if len(conditions) == 1:
        return conditions[0]
    return {"and": conditions}


def query_pages(filter_payload):
    """조건에 맞는 기록을 전부 읽어 온다. (페이지 목록, 오류안내문).

    `단원`처럼 옵션 목록에 아직 없는 값으로 거르면 노션이 400을 준다.
    그것은 "그 값으로 저장한 기록이 0건"이라는 뜻이므로 빈 목록으로 본다.
    """
    pages = []
    cursor = None
    while True:
        payload = {
            "filter": filter_payload,
            "sorts": [{"property": "기록일", "direction": "ascending"}],
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor
        res = notion_post(f"/data_sources/{DATA_SOURCE_ID}/query", NOTION_TOKEN, payload)
        if res.status_code == 400 and is_missing_option_error(res):
            return [], None
        if not res.ok:
            return None, explain_error(res)
        body = res.json()
        pages.extend(body.get("results", []))
        if not body.get("has_more"):
            break
        cursor = body.get("next_cursor")
    return pages, None


def fetch_blocks(block_id):
    """페이지(또는 블록) 아래의 블록을 children까지 전부 읽는다. (블록 목록, 오류안내문)."""
    result = []
    cursor = None
    while True:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        res = notion_get(path, NOTION_TOKEN)
        if not res.ok:
            return None, explain_error(res)
        body = res.json()
        for block in body.get("results", []):
            if block.get("has_children"):
                children, error = fetch_blocks(block["id"])
                if error:
                    return None, error
                block.setdefault(block.get("type"), {})["children"] = children
            result.append(block)
        if not body.get("has_more"):
            break
        cursor = body.get("next_cursor")
    return result, None


def export_sort_key(page):
    """단원 → 개념태그 → 기록일 순 (명세서 §16-2)."""
    return (
        property_value(page, "단원"),
        property_value(page, "개념태그"),
        (page.get("created_time") or ""),
    )


def record_markdown(page, blocks):
    """기록 하나를 명세서 §16-3의 순서대로 쓴다."""
    head = [f"## {property_value(page, '개념명') or '(제목 없음)'}"]

    first = " / ".join(
        value for value in (property_value(page, name) for name in EXPORT_KEEP_PROPERTIES) if value
    )
    source = " · ".join(
        value for value in (property_value(page, "교재구분"), property_value(page, "문제위치")) if value
    )
    second = " / ".join(
        value for value in (source, property_value(page, "참고출처"), property_value(page, "기록일")) if value
    )
    if first:
        head.append(f"- {first}")
    if second:
        head.append(f"- {second}")

    body = content_format.blocks_to_markdown(blocks)
    return "\n".join(head) + ("\n\n" + body if body else "")


def export_scope_name(data):
    """추출본 표의 `범위` 칸에 넣을 이름 — 전체 / 단원명 / 개념태그명 (명세서 §17-2)."""
    return data.get("개념태그") or data.get("단원") or "전체"


def export_scope_label(data):
    """화면과 파일 이름에 쓸 범위 이름."""
    if data.get("개념태그"):
        return f"{data['과목']}-{data['개념태그']}"
    if data.get("단원"):
        return f"{data['과목']}-{data['단원']}"
    return f"{data['과목']}-전체"


def safe_filename(name):
    for bad in FILENAME_BAD:
        name = name.replace(bad, "_")
    return "_".join(name.split())


def export_filename(data, part=None, total=1):
    stamp = datetime.now().strftime("%Y%m%d")
    tail = "" if total == 1 else f"_{part}of{total}"
    return safe_filename(f"cpa_{export_scope_label(data)}_{stamp}{tail}") + ".md"


def build_export_file(data, records):
    """머리말 + 기록들을 `---`로 이어 붙인다 (명세서 §16-3)."""
    header = [
        f"# {export_scope_label(data)}",
        f"- 내보낸 때: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 기록 수: {len(records)}건",
        "- 정렬: 단원 → 개념태그 → 기록일",
    ]
    return "\n".join(header) + "\n\n---\n\n" + "\n\n---\n\n".join(records) + "\n"


def split_export(data, records):
    """텔레그램 파일 크기 제한을 넘지 않게 나눈다. [(파일이름, 내용), ...].

    한 기록을 쪼개지 않고 기록 단위로만 나눈다. 마크다운 몇 MB는 현실적으로
    나오지 않지만, 넘었을 때 조용히 실패하는 것보다 나누는 편이 낫다.
    """
    whole = build_export_file(data, records)
    if len(whole.encode("utf-8")) <= TELEGRAM_FILE_LIMIT:
        return [(export_filename(data), whole)]

    groups = []
    current = []
    for record in records:
        candidate = current + [record]
        if current and len(build_export_file(data, candidate).encode("utf-8")) > TELEGRAM_FILE_LIMIT:
            groups.append(current)
            current = [record]
        else:
            current = candidate
    if current:
        groups.append(current)

    total = len(groups)
    return [
        (export_filename(data, part=i, total=total), build_export_file(data, group))
        for i, group in enumerate(groups, start=1)
    ]


# ------------------------------------------------------------
# 추출본 데이터베이스에 저장 (명세서 §16-5 · §17)
# ------------------------------------------------------------
# 학습 기록 표와 **절대 섞지 않는다.** 통합 정리본이 기록 표에 들어가면
# 개념태그 누적 횟수가 부풀고 보드 뷰마다 거대한 중복 페이지가 끼어든다 (§17-1).
# 그래서 여기서는 DATA_SOURCE_ID(기록 표)를 쓰지 않고 추출본 표의 것을 따로 쓴다.
def notion_parent_page_id(db_id):
    """표가 놓여 있는 부모 페이지의 id. (page_id, 오류안내문)."""
    res = notion_get(f"/databases/{db_id}", NOTION_TOKEN)
    if not res.ok:
        return None, explain_error(res)
    parent = res.json().get("parent", {})
    page_id = parent.get("page_id")
    if not page_id:
        return None, (
            "❌ 학습 기록 표가 어떤 페이지 안에 들어 있지 않아,\n"
            f"   그 아래에 `{EXPORT_DB_TITLE}` 표를 만들 자리를 찾지 못했습니다.\n"
            "   → 노션에서 표를 하나 만들고, 그 주소의 32자리 ID를\n"
            f"      .env의 {EXPORT_ENV_KEY}= 뒤에 붙여넣은 뒤 봇을 다시 켜 주세요."
        )
    return page_id, None


def ensure_export_db():
    """추출본 표의 data source id를 돌려준다 (명세서 §17-3). (ds_id, 오류안내문).

    .env에 ID가 있으면 그 표를 쓰고, 없으면 학습 기록 표의 부모 페이지 아래에
    새로 만들고 ID를 .env에 적어 둔다. 사용자가 따로 할 일은 없다.
    """
    global EXPORT_DB_ID, EXPORT_DATA_SOURCE_ID

    if EXPORT_DATA_SOURCE_ID:
        return EXPORT_DATA_SOURCE_ID, None

    if not EXPORT_DB_ID:
        parent_id, error = notion_parent_page_id(NOTION_DB_ID)
        if error:
            return None, error
        db_id, error = create_export_database(parent_id, NOTION_TOKEN)
        if error:
            return None, error
        EXPORT_DB_ID = db_id
        save_env_value(ENV_PATH, EXPORT_ENV_KEY, db_id)
        print(f"🆕 추출본 표 '{EXPORT_DB_TITLE}'을(를) 만들었습니다.")
        print(f"   .env의 {EXPORT_ENV_KEY}에 ID를 적어 두었습니다: {db_id}")

    ds_id, _, error = fetch_data_source(EXPORT_DB_ID, NOTION_TOKEN)
    if error:
        return None, error
    EXPORT_DATA_SOURCE_ID = ds_id
    return ds_id, None


def export_page_properties(data, count):
    """§17-2의 속성 5개. `생성일`은 노션이 자동으로 채우므로 보내지 않는다."""
    scope = export_scope_name(data)
    title = f"{data['과목']} › {scope} · {datetime.now().strftime('%Y-%m-%d')}"
    return {
        "제목": {"title": [{"type": "text", "text": {"content": title}}]},
        "과목": {"select": {"name": data["과목"]}},
        "범위": {"rich_text": [{"type": "text", "text": {"content": scope}}]},
        "포함건수": {"number": count},
    }


def save_export_page(data, records):
    """추출본 표에 통합 정리본 페이지 하나를 만든다. (url, 오류안내문, 알림목록).

    본문은 파일로 받는 것과 **글자 하나까지 같다** (명세서 §17-2). 블록이 100개를
    넘으면 send_record()와 같은 방식으로 나눠 이어붙인다.

    수식 폴백도 send_record()와 **같은 함수를 그대로 쓴다** (명세서 §13-3).
    추출본은 수십 건을 합친 것이라, 수식 하나가 400을 내서 통째로 날아가면
    손실이 기록 한 건과 비교할 수 없이 크다.
    """
    ds_id, error = ensure_export_db()
    if error:
        return None, error, []

    original = body_blocks(build_export_file(data, records))

    def create(blocks):
        return notion_post("/pages", NOTION_TOKEN, {
            "parent": {"type": "data_source_id", "data_source_id": ds_id},
            "properties": export_page_properties(data, len(records)),
            "children": blocks[:BLOCK_LIMIT],
        })

    # 1단계: 빈 수식·2000자 초과 수식은 보내기 전에 코드블록으로 바꾼다.
    blocks, fallback = content_format.replace_equations(
        original, content_format.is_unusable_equation
    )
    res = create(blocks)

    # 2단계: 그래도 400이면 수식 전부를 코드블록으로 바꿔 한 번 더 보낸다.
    if res.status_code == 400:
        retried, every = content_format.replace_equations(original, lambda block: True)
        if every:
            fallback = every
            blocks = retried
            res = create(blocks)

    notes = []
    notice = content_format.equation_notice(fallback)
    if notice:
        notes.append(notice)

    if not res.ok:
        return None, explain_error(res), []

    page = res.json()
    url = page.get("url")
    for start in range(BLOCK_LIMIT, len(blocks), BLOCK_LIMIT):
        group = blocks[start:start + BLOCK_LIMIT]
        res = notion_patch(f"/blocks/{page['id']}/children", NOTION_TOKEN, {"children": group})
        if not res.ok:
            return url, explain_error(res), notes
    return url, None, notes


# ------------------------------------------------------------
# /export 화면
# ------------------------------------------------------------
def export_items(export):
    """지금 단계에서 버튼으로 보여 줄 항목 (이름 목록)."""
    stage = export["stage"]
    data = export["data"]
    if stage == "subject":
        return list(CHAPTERS.keys())
    if stage == "category":
        return list(CHAPTERS[data["과목"]].keys())
    if stage == "chapter":
        return CHAPTERS[data["과목"]][data["대분류"]]
    if stage == "tag":
        return export["items"]
    return []


EXPORT_PROMPTS = {
    "subject": "어느 과목을 내보낼까요?",
    "scope": "범위를 골라 주세요.",
    "category": "대분류를 골라 주세요.",
    "chapter": "단원을 골라 주세요.",
    "tag": "개념태그를 골라 주세요.",
}


def build_export_text(export):
    lines = ["📤 마크다운 내보내기"]
    data = export["data"]
    chosen = [value for value in (data.get("과목"), data.get("대분류"), data.get("단원")) if value]
    lines.append("📍 " + (" › ".join(chosen) if chosen else "(아직 선택 전)"))
    lines.append("")
    lines.append(EXPORT_PROMPTS[export["stage"]])
    return "\n".join(lines)


def build_export_keyboard(export):
    rows = []
    if export["stage"] == "scope":
        rows = [[InlineKeyboardButton(label, callback_data=f"ex:scope:{key}")]
                for key, label in EXPORT_SCOPES]
    else:
        items = export_items(export)
        total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        if export["page"] >= total_pages:
            export["page"] = total_pages - 1
        shown = list(enumerate(items))
        if len(items) > PAGE_SIZE:
            start = export["page"] * PAGE_SIZE
            shown = shown[start:start + PAGE_SIZE]
        rows = [[InlineKeyboardButton(name, callback_data=f"ex:pick:{i}")] for i, name in shown]
        if total_pages > 1:
            rows.append([
                InlineKeyboardButton("◀ 이전", callback_data="ex:pg:-1"),
                InlineKeyboardButton(f"{export['page'] + 1}/{total_pages}", callback_data="noop"),
                InlineKeyboardButton("다음 ▶", callback_data="ex:pg:1"),
            ])
    rows.append([InlineKeyboardButton("❌ 취소", callback_data="ex:cancel")])
    return InlineKeyboardMarkup(rows)


async def show_export(context, chat_id, export, query=None):
    text = build_export_text(export)
    keyboard = build_export_keyboard(export)
    if query is not None:
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    sent = await context.bot.send_message(chat_id, text, reply_markup=keyboard)
    export["msg_id"] = sent.message_id


# ============================================================
# 핸들러
# ============================================================
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not NOTION_DB_ID:
        await update.message.reply_text(NOT_READY_TEXT)
        return
    if context.user_data.get("export") is not None:
        await update.message.reply_text(
            "⚠️ /export 를 진행하는 중입니다.\n"
            "그 화면의 [❌ 취소]를 누른 뒤 /new 로 시작해 주세요."
        )
        return
    context.user_data.pop("raw_wait", None)
    session = new_session()
    context.user_data["session"] = session
    await show(session, context, update.effective_chat.id)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not NOTION_DB_ID:
        await update.message.reply_text(NOT_READY_TEXT)
        return
    # /new 흐름과 섞이지 않게 한다 (명세서 §16-2).
    if context.user_data.get("session") is not None:
        await update.message.reply_text(
            "⚠️ /new 기록을 입력하는 중에는 /export를 쓸 수 없습니다.\n"
            "진행 중인 입력을 끝내거나 [❌ 처음부터]로 취소한 뒤 다시 시도해 주세요."
        )
        return
    context.user_data.pop("raw_wait", None)
    export = new_export()
    context.user_data["export"] = export
    await show_export(context, update.effective_chat.id, export)


async def collect_tag_options(export):
    """고른 과목에 실제로 쓰인 개념태그를 모은다. (태그 목록, 오류안내문)."""
    pages, error = await asyncio.to_thread(query_pages, export_filter(export["data"]))
    if error:
        return None, error
    names = []
    for page in pages:
        for option in page.get("properties", {}).get("개념태그", {}).get("multi_select") or []:
            name = option.get("name")
            if name and name not in names:
                names.append(name)
    return sorted(names), None


async def run_export(context, chat_id, export, query):
    """고른 범위의 기록을 모은 뒤, 받는 방법을 묻는다 (명세서 §16-2 · §16-5)."""
    data = export["data"]
    await query.edit_message_text(f"📤 {export_scope_label(data)} — 기록을 찾는 중입니다...")

    pages, error = await asyncio.to_thread(query_pages, export_filter(data))
    if error:
        await query.edit_message_text(build_failed_text("조회", error), reply_markup=FAILED_EXPORT_KEYBOARD)
        return
    if not pages:
        context.user_data.pop("export", None)
        await query.edit_message_text(
            f"📭 {export_scope_label(data)} 범위에 내보낼 기록이 없습니다.\n"
            "다른 범위로 /export 를 다시 시작해 주세요."
        )
        return

    pages.sort(key=export_sort_key)
    total = len(pages)
    records = []
    for number, page in enumerate(pages, start=1):
        blocks, error = await asyncio.to_thread(fetch_blocks, page["id"])
        if error:
            await query.edit_message_text(build_failed_text("본문 읽기", error), reply_markup=FAILED_EXPORT_KEYBOARD)
            return
        records.append(record_markdown(page, blocks))
        if number % 5 == 0 and number != total:
            await query.edit_message_text(f"📤 {total}건 중 {number}건 수집 중...")

    # 모으기까지가 여기다. 받는 방법은 사용자가 고른다 (명세서 §16-5).
    export["records"] = records
    export["stage"] = "deliver"
    await query.edit_message_text(build_deliver_text(total), reply_markup=DELIVER_KEYBOARD)


# 명세서 §16-5의 두 선택지. 통합 정리본은 수십 건을 합친 것이라 텔레그램
# 메시지 한도를 거의 확실히 넘으므로, [📄 노션에 저장]을 기본 경로로 안내한다.
DELIVER_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📥 파일로 받기", callback_data="ex:file"),
        InlineKeyboardButton("📄 노션에 저장", callback_data="ex:notion"),
    ],
    [InlineKeyboardButton("❌ 취소", callback_data="ex:cancel")],
])


def build_deliver_text(total):
    return (
        f"✅ {total}건을 모았습니다.\n\n"
        "📄 [노션에 저장]을 권합니다.\n"
        "   합친 정리본은 길어서 텔레그램에서 읽기 어렵고,\n"
        "   노션에 두면 과목·날짜로 찾아볼 수 있습니다."
    )


async def deliver_export_file(context, chat_id, export, query):
    """[📥 파일로 받기] — 마크다운 파일로 보낸다 (명세서 §16-2)."""
    data = export["data"]
    records = export["records"]
    files = split_export(data, records)
    note = "" if len(files) == 1 else f"\n(파일이 커서 {len(files)}개로 나눴습니다)"
    await query.edit_message_text(f"📤 {len(records)}건의 파일을 보냅니다...{note}")

    for name, text in files:
        document = BytesIO(text.encode("utf-8"))
        document.name = name
        await context.bot.send_document(chat_id, document=document, filename=name)

    context.user_data.pop("export", None)
    # 명세서 §16-6 — 예전 문구("파일을 그대로 다시 붙여넣으면...")는 파일을 봇에
    # 되보내면 저장된다는 오해를 낳았다. 실제로는 "먼저 /new 로..."가 떴다.
    await context.bot.send_message(
        chat_id,
        "📥 마크다운 파일로 받았습니다.\n"
        "읽기용 파일이며, 봇에 다시 보내도 저장되지 않습니다.\n"
        "노션에 모아 두려면 [📄 노션에 저장]을 이용하십시오.",
    )


async def deliver_export_notion(context, chat_id, export, query):
    """[📄 노션에 저장] — 추출본 표에 페이지 하나를 만든다 (명세서 §16-5 · §17)."""
    data = export["data"]
    records = export["records"]
    await query.edit_message_text(f"📄 {len(records)}건을 노션 추출본 표에 저장하는 중입니다...")

    url, error, notes = await asyncio.to_thread(save_export_page, data, records)

    if error and url is None:
        await query.edit_message_text(
            build_failed_text("추출본 저장", error), reply_markup=FAILED_EXPORT_KEYBOARD
        )
        return
    if error:
        # 페이지는 만들어졌지만 뒷부분을 이어붙이다 실패했다. 성공처럼 알리지 않는다.
        context.user_data.pop("export", None)
        await query.edit_message_text(
            "⚠️ 일부만 저장되었습니다.\n"
            "내용이 길어 여러 번에 나눠 보내는 중에 실패했습니다.\n\n"
            f"{error}\n\n"
            f"만들어진 페이지: {url}"
        )
        return

    context.user_data.pop("export", None)
    text = (
        f"✅ {export_scope_label(data)} {len(records)}건을 노션에 저장했습니다.\n"
        f"표 이름: {EXPORT_DB_TITLE}\n"
        f"{url}"
    )
    if notes:
        # 수식이 코드블록으로 바뀐 안내 (명세서 §13-3). 저장 자체는 성공이다.
        text += "\n\n" + "\n".join(notes)
    await query.edit_message_text(text)


async def on_export_button(update, context, action):
    """/export 화면의 버튼. /new 흐름(session)과 완전히 분리되어 있다."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    export = context.user_data.get("export")
    if export is None:
        await query.edit_message_text("끝난 화면입니다. /export 로 다시 시작해 주세요.")
        return

    if action == "ex:cancel":
        context.user_data.pop("export", None)
        await query.edit_message_text("❌ 내보내기를 취소했습니다. /export 로 다시 시작할 수 있습니다.")
        return

    if action in ("ex:file", "ex:notion"):
        if not export.get("records"):
            await query.edit_message_text("끝난 화면입니다. /export 로 다시 시작해 주세요.")
            return
        if action == "ex:file":
            await deliver_export_file(context, chat_id, export, query)
        else:
            await deliver_export_notion(context, chat_id, export, query)
        return

    if action.startswith("ex:pg:"):
        items = export_items(export)
        total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        export["page"] = (export["page"] + int(action[6:])) % total_pages
        await show_export(context, chat_id, export, query)
        return

    if action.startswith("ex:scope:"):
        scope = action[9:]
        export["page"] = 0
        if scope == "all":
            await run_export(context, chat_id, export, query)
            return
        if scope == "chapter":
            export["stage"] = "category"
            await show_export(context, chat_id, export, query)
            return
        await query.edit_message_text("🏷 이 과목에 쓰인 개념태그를 읽는 중입니다...")
        names, error = await collect_tag_options(export)
        if error:
            await query.edit_message_text(build_failed_text("조회", error), reply_markup=FAILED_EXPORT_KEYBOARD)
            return
        if not names:
            context.user_data.pop("export", None)
            await query.edit_message_text(
                f"📭 {export['data']['과목']} 과목에 기록이 아직 없습니다.\n"
                "/export 로 다시 시작해 주세요."
            )
            return
        export["items"] = names
        export["stage"] = "tag"
        await show_export(context, chat_id, export, query)
        return

    if action.startswith("ex:pick:"):
        items = export_items(export)
        index = int(action[8:])
        if index >= len(items):
            return
        picked_name = items[index]
        stage = export["stage"]
        export["page"] = 0

        if stage == "subject":
            export["data"]["과목"] = picked_name
            export["stage"] = "scope"
            await show_export(context, chat_id, export, query)
            return
        if stage == "category":
            export["data"]["대분류"] = picked_name
            export["stage"] = "chapter"
            await show_export(context, chat_id, export, query)
            return
        if stage == "chapter":
            export["data"]["단원"] = f"{export['data']['대분류']}-{picked_name}"
            await run_export(context, chat_id, export, query)
            return
        if stage == "tag":
            export["data"]["개념태그"] = picked_name
            await run_export(context, chat_id, export, query)
            return


FAILED_EXPORT_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ 닫기", callback_data="ex:cancel")],
])


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    query = update.callback_query
    # 텔레그램 앱의 로딩 표시를 지우는 보조 동작이다. 실패해도 아래 본 처리를
    # 막지 않는다. 예전에는 이 줄이 맨 앞에 그냥 있어서, 순간적인 네트워크
    # 끊김(NetworkError)이 나면 버튼 처리가 시작도 못 하고 통째로 중단됐다.
    # 사용자에게는 알리지 않는다. 로딩 표시가 잠깐 더 도는 정도의 일이다.
    try:
        await query.answer()
    except Exception as e:
        print(f"[안내] 버튼의 로딩 표시를 지우지 못했습니다(처리는 그대로 이어갑니다): {e}")

    # 재시작 알림 버튼은 입력 흐름이 없어도 눌릴 수 있으므로 먼저 처리한다.
    if query.data.startswith("pnd:"):
        await on_pending_button(update, context, query.data)
        return

    # /export는 /new와 다른 상태를 쓰므로 session을 보기 전에 갈라낸다 (명세서 §16-2).
    if query.data.startswith("ex:"):
        await on_export_button(update, context, query.data)
        return

    # /setup도 마찬가지다. 표를 고르는 동안에는 session이 아예 없다.
    if query.data.startswith("st:"):
        await on_setup_button(update, context, query.data)
        return

    session = context.user_data.get("session")
    if session is None:
        await query.edit_message_text("흐름이 끝난 화면입니다. /new 로 다시 시작해 주세요.")
        return

    action = query.data
    chat_id = update.effective_chat.id

    if action == "noop":
        return

    if action == "reset":
        context.user_data.pop("session", None)
        await query.edit_message_text("❌ 입력을 취소했습니다. /new 로 다시 시작할 수 있습니다.")
        return

    # 저장을 끝냈거나 저장에 실패한 뒤의 화면
    if action == "retry":
        if session["step"] >= len(STEPS):
            await do_save(session, context, chat_id, query)
        else:
            await show(session, context, chat_id, query)
        return

    if action == "again":
        restart_same_problem(session)
        await show(session, context, chat_id, query)
        return

    if action == "editsource":
        # 화면에 보이는 조건과 똑같이 막는다. (오래된 화면의 버튼을 눌렀을 때 대비)
        if session["step"] >= len(STEPS) or not session["locked"] or session["step"] < TAG_STEP:
            return
        if STEPS[session["step"]] == "tag" and session["tag_mode"] is not None:
            return
        edit_source(session)
        await show(session, context, chat_id, query)
        return

    if action == "done":
        context.user_data.pop("session", None)
        await query.edit_message_text("🏁 기록을 마쳤습니다. 다음 기록은 /new 로 시작해 주세요.")
        return

    # 손상 감지 화면의 두 버튼 (명세서 §15-2).
    # 보낸 내용은 세션에 그대로 있으므로 어느 쪽을 골라도 유실되지 않는다.
    if action in ("force", "damagecancel"):
        if session["damage"] is None:
            return
        session["damage"] = None
        if action == "force":
            if advance(session):
                await do_save(session, context, chat_id, query)
            else:
                await show(session, context, chat_id, query)
            return
        session["notice"] = (
            "ℹ️ 저장하지 않았습니다.\n"
            "   컴퓨터 텔레그램에서 정리 내용을 다시 보내 주세요."
        )
        await show(session, context, chat_id, query)
        return

    if session["step"] >= len(STEPS):
        # 저장이 끝난 기록의 옛 입력 화면에 남아 있는 버튼을 누른 경우다.
        # 예전에는 조용히 무시해서 [⬅️ 뒤로]가 아무 반응이 없는 것처럼 보였다
        # (화면 정리에 실패해 옛 화면이 지워지지 않고 남았을 때).
        await query.edit_message_text(
            "이미 저장이 끝난 기록의 지난 화면입니다.\n"
            "같은 내용을 다시 보내면 노션에 두 번 저장됩니다.\n"
            "노션을 확인해 보시고 /new 로 다음 기록을 시작해 주세요."
        )
        return

    step = STEPS[session["step"]]

    if action == "back":
        # 태그의 하위 화면(입력·검색·대표·빼기)에서는 태그 목록으로만 되돌린다.
        if step == "tag" and session["tag_mode"]:
            session["tag_mode"] = None
            clamp_page(session, len(tag_candidates(session)))
            await show(session, context, chat_id, query)
            return
        if session["step"] == 0:
            context.user_data.pop("session", None)
            await query.edit_message_text("⬅️ 첫 화면이라 더 뒤로 갈 수 없어 흐름을 끝냈습니다. /new 로 다시 시작해 주세요.")
            return
        step_back(session)
        session["page"] = 0
        reset_tag_screen(session)
        if session["again"] and session["step"] < CHAPTER_STEP:
            release_keep(session)
            session["notice"] = "ℹ️ 처음 단계로 돌아가 유지하던 출처 정보를 해제했습니다."
        # 되돌아간 단계부터 뒤쪽 값은 모두 비워 다시 고를 수 있게 한다.
        clear_from(session, session["step"])
        await show(session, context, chat_id, query)
        return

    if action.startswith("pg:"):
        count = len(tag_candidates(session)) if step == "tag" else len(options_of(session))
        total_pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        session["page"] = (session["page"] + int(action[3:])) % total_pages
        await show(session, context, chat_id, query)
        return

    if action in ("newtag", "tagsearch", "tagstar", "tagunpick"):
        # 검색어(tag_query)는 여기서 건드리지 않는다. [➕ 새 태그]를 거쳐 돌아와도
        # 검색 상태가 유지되어야 한다 (명세서 §12-3).
        if action in ("tagstar", "tagunpick") and not picked(session, "개념태그"):
            return
        session["tag_mode"] = {"newtag": "input", "tagsearch": "search"}.get(action, action[3:])
        await show(session, context, chat_id, query)
        return

    if action == "tagclear":
        session["tag_query"] = None
        session["page"] = 0
        await show(session, context, chat_id, query)
        return

    if action.startswith("tag:"):
        items = tag_candidates(session)
        index = int(action[4:])
        if index >= len(items):
            return
        name = items[index][0]
        toggle(session, "개념태그", name)
        # 고른 태그는 버튼 목록에서 빠지므로 쪽 수가 줄어들 수 있다.
        clamp_page(session, len(tag_candidates(session)))
        await show(session, context, chat_id, query)
        return

    if action.startswith("star:"):
        chosen = picked(session, "개념태그")
        index = int(action[5:])
        if index >= len(chosen):
            return
        session["data"]["대표태그"] = chosen[index]
        session["tag_mode"] = None
        session["notice"] = f"⭐ 대표 태그를 #{chosen[index]} 로 바꿨습니다. 제목에 이 태그가 쓰입니다."
        await show(session, context, chat_id, query)
        return

    if action.startswith("unpick:"):
        chosen = picked(session, "개념태그")
        index = int(action[7:])
        if index >= len(chosen):
            return
        name = chosen[index]
        toggle(session, "개념태그", name)
        if not picked(session, "개념태그"):
            session["tag_mode"] = None
        clamp_page(session, len(tag_candidates(session)))
        session["notice"] = f"🗑 #{name} 을(를) 선택에서 뺐습니다."
        await show(session, context, chat_id, query)
        return

    if action == "multidone":
        field = FIELDS[step]
        if not picked(session, field):
            if step in SKIPPABLE:
                # 하나도 고르지 않고 완료를 누르면 건너뛴 것과 같게 다룬다.
                session["data"][field] = None
            else:
                session["notice"] = (
                    f"⚠️ {field}는 최소 하나가 필요합니다.\n"
                    "   아래 버튼에서 고르거나 [➕ 새 태그]로 만들어 주세요."
                )
                await show(session, context, chat_id, query)
                return
        if advance(session):
            await do_save(session, context, chat_id, query)
        else:
            await show(session, context, chat_id, query)
        return

    if action == "skip":
        session["data"][FIELDS[step]] = None
        if advance(session):
            await do_save(session, context, chat_id, query)
        else:
            await show(session, context, chat_id, query)
        return

    if action.startswith("pick:"):
        items = options_of(session)
        index = int(action[5:])
        if index >= len(items):
            return
        if step in MULTI_STEPS:
            toggle(session, FIELDS[step], items[index])
            await show(session, context, chat_id, query)
            return
        session["data"][FIELDS[step]] = items[index]
        if advance(session):
            await do_save(session, context, chat_id, query)
        else:
            await show(session, context, chat_id, query)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if context.user_data.get("raw_wait"):
        await do_raw_analysis(update, context)
        return
    if context.user_data.get("export") is not None:
        await update.message.reply_text("지금은 /export 화면의 버튼을 눌러 주세요.")
        return
    session = context.user_data.get("session")
    if session is None:
        await update.message.reply_text("먼저 /new 로 기록을 시작해 주세요.")
        return

    if session["step"] >= len(STEPS):
        await update.message.reply_text("지금은 위 화면의 버튼을 눌러 주세요.")
        return

    step = STEPS[session["step"]]
    tag_typing = step == "tag" and session["tag_mode"] in ("input", "search")
    if step not in TEXT_STEPS and not tag_typing:
        await update.message.reply_text("지금은 위 화면의 버튼을 눌러 주세요.")
        return

    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("빈 내용은 저장할 수 없습니다. 다시 입력해 주세요.")
        return

    chat_id = update.effective_chat.id

    if step == "tag" and session["tag_mode"] == "search":
        # 검색어는 세션에 남겨 둔다. [➕ 새 태그]를 거쳐 돌아와도 유지되어야 한다 (명세서 §12-3).
        session["tag_query"] = value
        session["tag_mode"] = None
        session["page"] = 0
        if not [item for item in (session["tags"] or []) if value in item[0]]:
            session["notice"] = (
                f"🔍 '{value}'이(가) 들어간 태그가 이 단원에 없습니다.\n"
                "   [➕ 새 태그]로 새로 만들거나, [🔎 검색 해제]로 전체 목록을 보세요."
            )
        await show(session, context, chat_id)
        return

    if step == "tag" and session["tag_mode"] == "input":
        # 쉼표는 태그를 나누는 구분자다. 개별 태그 이름에 쉼표가 남으면 노션이 거부한다.
        names = split_tags(value)
        if not names:
            await update.message.reply_text("태그 이름이 비어 있습니다. 다시 입력해 주세요.")
            return
        # 새로 넣은 태그는 자동으로 고른 상태가 되고, 이미 고른 태그와 합쳐진다.
        known = {name for name, _ in (session["tags"] or [])}
        for name in names:
            if name not in known:
                # 이 단원의 태그 목록에도 넣어 둔다. 그래야 [🗑 태그 빼기]로 뺐을 때
                # 버튼 목록에 다시 나타나 되고를 수 있다. 누적 횟수는 아직 0이다.
                session["tags"] = (session["tags"] or []) + [(name, 0)]
                known.add(name)
            if name not in picked(session, "개념태그"):
                toggle(session, "개념태그", name)
        session["tag_mode"] = None
        clamp_page(session, len(tag_candidates(session)))
        session["notice"] = f"➕ 태그 {len(names)}개를 넣었습니다: " + " ".join(f"#{n}" for n in names)
        await show(session, context, chat_id)
        return

    session["data"][FIELDS[step]] = value

    # 정리 내용만 검사한다. 서식이 들어가는 칸은 여기 하나뿐이다 (명세서 §15-2).
    # 값은 이미 세션에 넣어 두었으므로 [🔁 그래도 저장]을 누르면 그대로 저장된다.
    if step == "content":
        reason = content_format.detect_corruption(value)
        if reason:
            session["damage"] = reason
            await send(session, context, chat_id, build_damage_text(reason), DAMAGE_KEYBOARD)
            return

    if advance(session):
        await do_save(session, context, chat_id)
    else:
        await show(session, context, chat_id)


# ============================================================
# 전역 오류 처리
# ============================================================
# 버튼을 눌렀을 때뿐 아니라 글을 보낸 뒤에도 뜨는 안내다. 특정 버튼을 다시
# 누르라고 하지 않는다. 저장 단계에서 났다면 노션에 이미 들어갔을 수 있으므로,
# 다시 시도하기 전에 노션을 확인하라고 먼저 안내한다 (중복 저장 방지).
UNEXPECTED_TEXT = (
    "⚠️ 예상치 못한 오류가 발생했습니다.\n"
    "입력하신 내용은 그대로 남아 있습니다.\n\n"
    "저장 단계에서 생긴 오류라면 노션에는 이미 들어갔을 수 있습니다.\n"
    "같은 내용을 다시 보내기 전에 노션을 먼저 확인해 주세요.\n"
    "이미 있다면 다시 보내지 마시고 /new 로 다음 기록을 시작하세요.\n\n"
    "(오류 원문은 봇을 켜 둔 터미널 창에 적혀 있습니다.)"
)

# 아직 저장을 시도하지도 않은 단계(작성 중)에서 난 오류의 안내.
# 노션에 들어간 것이 없으므로 노션을 확인하라고 말하지 않는다. 예전에는 이런
# 경우에도 위 문구가 떠서, 저장과 무관한 단계인데 노션을 뒤지게 만들었다.
UNEXPECTED_DRAFT_TEXT = (
    "⚠️ 예상치 못한 오류가 발생했습니다.\n"
    "아직 저장하기 전 단계라 저장된 내용은 없습니다.\n"
    "지금까지 입력하신 내용은 그대로 남아 있습니다.\n\n"
    "바로 아래에 지금 단계 화면을 다시 띄웁니다. 이어서 진행해 주세요.\n\n"
    "(오류 원문은 봇을 켜 둔 터미널 창에 적혀 있습니다.)"
)

# 화면을 다시 띄우는 것까지 실패했을 때만 쓴다. 이때는 이어갈 방법이 없다.
RESHOW_FAILED_TEXT = (
    "⚠️ 지금 단계 화면을 다시 띄우지 못했습니다.\n"
    "이어서 진행할 수 없으니 /new 로 다시 시작해 주세요."
)


def save_attempted(session):
    """이 세션이 노션 저장을 시도한 적이 있는가. (새 상태를 만들지 않고 기존 값만 본다)

    - step이 마지막 단계를 넘어섰다 = advance()가 끝나 do_save로 들어간 상태다.
    - saved / pending_id는 do_save가 실제로 save_record를 부른 뒤에만 채워진다.
      (필수값이 비어 되돌아간 경우에는 save_record를 부르기 전에 빠져나오므로
       둘 다 그대로다 = 저장 시도 아님)
    """
    return (
        session["step"] >= len(STEPS)
        or session["saved"]
        or session["pending_id"] is not None
    )


async def reshow(session, context, chat_id):
    """오류 뒤 지금 단계 화면을 그대로 다시 그린다. 세션 값은 건드리지 않는다.

    저장을 시도한 세션은 부르는 쪽에서 미리 걸러내므로, 여기서 do_save가 다시
    돌아 노션에 중복 저장될 일은 없다. 이 함수는 화면만 다룬다.
    """
    if session["damage"] is not None:
        # 손상 확인 화면(명세서 §15-2)에 서 있던 경우다. 보통의 단계 화면을 띄우면
        # [🔁 그래도 저장]을 고를 방법이 사라진다.
        await send(session, context, chat_id, build_damage_text(session["damage"]), DAMAGE_KEYBOARD)
        return
    await show(session, context, chat_id)


async def on_error(update, context):
    """어디서든 새어 나온 예외를 받는다. 사용자 화면에 영어 원문을 보이지 않는다.

    세션(context.user_data)은 손대지 않으므로 지금까지 입력한 값은 그대로 남는다.
    작성 중이었다면 안내만 하지 않고 지금 단계 화면까지 다시 띄운다. 예전에는
    "내용이 남아 있다"고만 안내해 놓고 화면을 갱신하지 않아, 결국 /new 로
    처음부터 다시 쓸 수밖에 없었다.
    """
    print("[안내] 예상치 못한 오류가 발생했습니다. 아래는 개발용 오류 원문입니다.")
    traceback.print_exception(
        type(context.error), context.error, getattr(context.error, "__traceback__", None)
    )

    # Update가 아닌 것이 넘어올 수도 있으므로 getattr로 안전하게 꺼낸다.
    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return

    # /export·/setup에는 session이 없다. 저장 시도 여부를 알 수 없으므로
    # 그때는 예전의 포괄적인 문구를 그대로 쓴다.
    user_data = getattr(context, "user_data", None) or {}
    session = user_data.get("session")
    drafting = session is not None and not save_attempted(session)

    try:
        await context.bot.send_message(chat.id, UNEXPECTED_DRAFT_TEXT if drafting else UNEXPECTED_TEXT)
    except Exception as e:
        # 안내조차 못 보냈다면 여기서 멈춘다. 오류 처리기가 또 오류를 내면 안 된다.
        print(f"[안내] 오류 안내를 텔레그램으로 보내지 못했습니다: {e}")
        return

    if not drafting:
        return

    try:
        await reshow(session, context, chat.id)
    except Exception as e:
        print(f"[안내] 오류 뒤 지금 단계 화면을 다시 띄우지 못했습니다: {e}")
        try:
            await context.bot.send_message(chat.id, RESHOW_FAILED_TEXT)
        except Exception as e2:
            print(f"[안내] 다시 시작하라는 안내도 보내지 못했습니다: {e2}")


# ============================================================
# 시작
# ============================================================
def main():
    global CHAPTERS, OPTIONS, ALLOWED_USER_ID, NOTION_TOKEN, DATA_SOURCE_ID
    global NOTION_DB_ID, EXPORT_DB_ID

    load_dotenv(ENV_PATH, encoding="utf-8")

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        die("텔레그램 봇 토큰이 없습니다.\n   .env 파일의 TELEGRAM_BOT_TOKEN에 BotFather에서 받은 토큰을 넣어주세요.")

    allowed_raw = (os.getenv("TELEGRAM_ALLOWED_USER_ID") or "").strip()
    if not allowed_raw:
        die("허용할 사용자 ID를 .env에 넣어주세요.\n   .env 파일의 TELEGRAM_ALLOWED_USER_ID 항목입니다.")
    if not allowed_raw.lstrip("-").isdigit():
        die(f"TELEGRAM_ALLOWED_USER_ID는 숫자여야 합니다. 지금 값: {allowed_raw}")
    ALLOWED_USER_ID = int(allowed_raw)

    NOTION_TOKEN = (os.getenv("NOTION_TOKEN") or "").strip()
    if not NOTION_TOKEN:
        die(".env 파일에 NOTION_TOKEN이 없습니다.\n   https://www.notion.so/my-integrations 에서 토큰을 복사해 넣어주세요.")

    # 명세서 §7 "대상 DB 지정 (2단 방식)" — 값이 있으면 그대로 쓰고,
    # 비어 있으면 봇을 띄운 뒤 /setup 으로 고르게 한다. 여기서 죽이지 않는다.
    NOTION_DB_ID = (os.getenv("NOTION_DB_ID") or "").strip()

    # 추출본 표는 없어도 정상이다. 처음 [📄 노션에 저장]을 누를 때 만든다 (명세서 §17-3).
    EXPORT_DB_ID = (os.getenv(EXPORT_ENV_KEY) or "").strip()

    CHAPTERS = load_chapters()
    chapter_count = sum(len(v) for cats in CHAPTERS.values() for v in cats.values())

    print("[봇] 봇을 시작합니다.")
    print(f"   과목 {len(CHAPTERS)}개 / 단원 {chapter_count}개를 chapters.yaml에서 읽었습니다.")

    if NOTION_DB_ID:
        DATA_SOURCE_ID, OPTIONS = load_notion_options(NOTION_DB_ID, NOTION_TOKEN)
        counts = " / ".join(f"{name} {len(OPTIONS[name])}" for name in NOTION_OPTION_PROPERTIES)
        print(f"   노션에서 옵션을 읽었습니다 — {counts}")
        print(f"   허용 사용자 ID: {ALLOWED_USER_ID}")
        print("   텔레그램에서 /new 를 보내 시작하세요. (끄려면 이 창에서 Ctrl+C)")
    else:
        print("   [안내] .env의 NOTION_DB_ID가 비어 있습니다.")
        print(f"   허용 사용자 ID: {ALLOWED_USER_ID}")
        print("   텔레그램에서 /setup 을 보내 저장할 표를 골라 주세요. (끄려면 이 창에서 Ctrl+C)")

    app = Application.builder().token(token).post_init(notify_pending).build()
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("raw", cmd_raw))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
