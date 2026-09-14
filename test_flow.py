"""test_flow.py - 텔레그램과 노션 없이 bot.py의 입력 흐름을 검사한다.

    실행:  .venv/bin/python test_flow.py

가짜 텔레그램(버튼을 누르고 글자를 보내는 시늉)과 가짜 노션(보낸 내용을 받아
적어 두기만 하는 함수)을 만들어, bot.py의 핸들러를 **실제 코드 그대로** 부른다.
바꿔치는 것은 노션을 부르는 두 함수(fetch_tags·send_record)와 백업 파일의 위치뿐이다.

과목·단원 목록과 선택지는 chapters.yaml·노션 대신 아래 고정값을 쓴다. 그래야
교재 목차를 고쳐도 검사 결과가 흔들리지 않는다.

외부 시험 도구를 쓰지 않으므로 requirements.txt는 그대로다.
전부 통과하면 종료 코드 0, 하나라도 실패하면 1이다.
"""

import asyncio
import copy
import os
import tempfile
import traceback
from datetime import datetime
from types import SimpleNamespace

import bot
import check_notion
import content_format
import notion_common
import setup_notion

USER_ID = 424242
CHAT_ID = 9999

# ============================================================
# 고정값 (chapters.yaml·노션을 대신한다)
# ============================================================
CHAPTERS = {
    "세법": {"부가가치세": ["ch06.매입세액과 납부세액", "ch07.겸영사업자 세액계산"]},
    "회계": {"중회": ["제05장 재고자산과 농림어업자산"]},
}

OPTIONS = {
    "지식유형": ["기준서", "법령", "이론·개념", "산식", "암기", "견해"],
    "오답유형": ["개념 미숙지", "개념 혼동", "조건 오독", "계산 실수", "함정 미인지", "시간부족", "근거오류"],
    "교재구분": ["기출", "객관식", "연습서", "모의고사", "AI질의"],
}

# 이 단원에 이미 쌓여 있다고 가정하는 태그 14개. 10개를 넘겨야 쪽 넘김을 볼 수 있다.
CHAPTER_TAGS = [
    ("공통매입세액", 4),
    ("안분계산", 3),
    ("환급세액재계산", 2),
    ("실지귀속구분불가", 1),
    ("예정신고기간매입", 1),
] + [(f"여분태그{i:02}", 1) for i in range(1, 13)]

# 보낸 내용이 여기에 쌓인다. 검사는 이 목록을 들여다본다.
SENT = []


def fake_fetch_tags(chapter):
    return list(CHAPTER_TAGS), None


def fake_send_record(item):
    SENT.append(copy.deepcopy(item))
    # bot.send_record와 같은 모양으로 돌려준다 (url, 오류, 일부만저장, 알림목록).
    return "https://notion.example/TEST", None, False, []


def install_fakes():
    bot.CHAPTERS = CHAPTERS
    bot.OPTIONS = OPTIONS
    bot.ALLOWED_USER_ID = USER_ID
    bot.DATA_SOURCE_ID = "TEST_DATA_SOURCE"
    bot.NOTION_TOKEN = "TEST_TOKEN"
    # .env에 표 ID가 들어 있는 상태(= 배포 후 정상 상태)를 흉내 낸다.
    # 비어 있으면 /new·/export가 /setup 안내만 내보낸다 (명세서 §7).
    bot.NOTION_DB_ID = "TEST_DB_ID"
    bot.fetch_tags = fake_fetch_tags
    bot.send_record = fake_send_record
    # 실제 백업 파일(pending_records.json)에는 손대지 않는다.
    bot.PENDING_PATH = os.path.join(tempfile.mkdtemp(prefix="cpa_test_"), "pending_records.json")


# ============================================================
# 가짜 텔레그램
# ============================================================
class Screen:
    """지금 사용자에게 보이는 화면 하나. 텔레그램 대신 여기에 그린다."""

    def __init__(self):
        self.text = ""
        self.markup = None
        self.replies = []
        self.user_data = {}
        self.msg_id = 0
        self.documents = []


class FakeBot:
    def __init__(self, screen):
        self.screen = screen

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        # /raw는 parse_mode="HTML"을 함께 넘긴다. 받아 두기만 하면 된다.
        self.screen.text = text
        self.screen.markup = reply_markup
        self.screen.msg_id += 1
        return SimpleNamespace(message_id=self.screen.msg_id)

    async def send_document(self, chat_id, document=None, filename=None):
        self.screen.documents.append((filename, document.getvalue().decode("utf-8")))

    async def delete_message(self, chat_id, message_id):
        pass


class FakeQuery:
    def __init__(self, screen, data):
        self.screen = screen
        self.data = data

    async def answer(self):
        pass

    async def edit_message_text(self, text, reply_markup=None):
        self.screen.text = text
        self.screen.markup = reply_markup


class FakeMessage:
    def __init__(self, screen, text, entities=None):
        self.screen = screen
        self.text = text
        # /raw가 텔레그램의 서식 정보를 들여다본다 (명세서 §14-1).
        self.entities = entities or []

    async def reply_text(self, text, **kwargs):
        self.screen.replies.append(text)


class Flow:
    """버튼을 누르고 글자를 보내는 사용자 한 명."""

    def __init__(self):
        self.screen = Screen()
        self.context = SimpleNamespace(user_data=self.screen.user_data, bot=FakeBot(self.screen))

    def _update(self, query=None, message=None):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=USER_ID, full_name="테스트 사용자"),
            effective_chat=SimpleNamespace(id=CHAT_ID),
            callback_query=query,
            message=message,
        )

    async def start(self):
        await bot.cmd_new(self._update(), self.context)

    async def press(self, label):
        button = self.find(label)
        await bot.on_button(self._update(query=FakeQuery(self.screen, button.callback_data)), self.context)

    async def say(self, text):
        await bot.on_text(self._update(message=FakeMessage(self.screen, text)), self.context)

    async def command(self, handler):
        await handler(self._update(message=FakeMessage(self.screen, "")), self.context)

    @property
    def documents(self):
        return self.screen.documents

    @property
    def session(self):
        return self.screen.user_data.get("session")

    @property
    def text(self):
        return self.screen.text

    def labels(self):
        if self.screen.markup is None:
            return []
        return [b.text for row in self.screen.markup.inline_keyboard for b in row]

    def find(self, label):
        if self.screen.markup is not None:
            for row in self.screen.markup.inline_keyboard:
                for button in row:
                    if label in button.text:
                        return button
        raise AssertionError(
            f"화면에 [{label}] 버튼이 없습니다.\n"
            f"      지금 버튼: {self.labels()}\n"
            f"      지금 화면: {self.text!r}"
        )

    def has(self, label):
        return any(label in one for one in self.labels())


# ============================================================
# 검사 도구
# ============================================================
def check(condition, message):
    if not condition:
        raise AssertionError(message)


def last_properties():
    check(SENT, "노션으로 보낸 기록이 없습니다.")
    return SENT[-1]["속성"]


def title_of(properties):
    return "".join(piece["text"]["content"] for piece in properties["개념명"]["title"])


def select_of(properties, name):
    return properties[name]["select"]["name"]


def multi_of(properties, name):
    return [option["name"] for option in properties[name]["multi_select"]]


def text_of(properties, name):
    return "".join(piece["text"]["content"] for piece in properties[name]["rich_text"])


async def walk_to_tag_step(flow, chapter="ch06.매입세액과 납부세액"):
    """과목 → 대분류 → 단원까지 골라 개념태그 화면에 선다."""
    await flow.start()
    await flow.press("세법")
    await flow.press("부가가치세")
    await flow.press(chapter)
    check("개념태그" in flow.text, f"개념태그 화면이 아닙니다: {flow.text!r}")


CASES = []


def case(name):
    def decorate(func):
        CASES.append((name, func))
        return func
    return decorate


# ============================================================
# 1. 전체 흐름 완주
# ============================================================
@case("전체 흐름 완주 → 저장 내용과 개념명")
async def test_full_flow():
    flow = Flow()
    await walk_to_tag_step(flow)
    await flow.press("#공통매입세액")
    await flow.press("✔️ 선택 완료")
    await flow.press("법령")
    await flow.press("계산 실수")
    await flow.press("✔️ 선택 완료")
    await flow.press("연습서")
    await flow.say("연p.212#15")
    await flow.say("기준서 1116호 문단 22")
    await flow.say("공통매입세액은 과세·면세 공급가액 비율로 안분한다.")

    check("✅ 저장 완료" in flow.text, f"저장 완료 화면이 아닙니다: {flow.text!r}")
    check(len(SENT) == 1, f"기록이 1건 저장되어야 하는데 {len(SENT)}건입니다.")

    properties = last_properties()
    check(title_of(properties) == "공통매입세액 · 연p.212#15",
          f"개념명이 다릅니다: {title_of(properties)!r}")
    check(select_of(properties, "과목") == "세법", "과목이 다릅니다.")
    check(select_of(properties, "단원") == "부가가치세-ch06.매입세액과 납부세액", "단원이 다릅니다.")
    check(multi_of(properties, "개념태그") == ["공통매입세액"], "개념태그가 다릅니다.")
    check(select_of(properties, "지식유형") == "법령", "지식유형이 다릅니다.")
    check(multi_of(properties, "오답유형") == ["계산 실수"], "오답유형이 다릅니다.")
    check(select_of(properties, "교재구분") == "연습서", "교재구분이 다릅니다.")
    check(text_of(properties, "문제위치") == "연p.212#15", "문제위치가 다릅니다.")
    check(text_of(properties, "참고출처") == "기준서 1116호 문단 22", "참고출처가 다릅니다.")

    old = [name for name in ("출처구분", "문제출처", "근거") if name in properties]
    check(not old, f"옛 속성 이름이 그대로 보내졌습니다: {old}")

    body = SENT[-1]["정리 내용"]
    check("안분한다" in body, f"정리 내용이 다릅니다: {body!r}")


# ============================================================
# 2. 개념태그 다중 선택 · 해제 · 대표 지정 · 쪽 넘김
# ============================================================
@case("개념태그 다중 선택·해제·대표 지정·쪽 넘김")
async def test_tag_screen():
    flow = Flow()
    await walk_to_tag_step(flow)

    check("선택: (아직 없음)" in flow.text, f"선택 줄이 없습니다: {flow.text!r}")
    check(flow.has("1/2"), f"쪽 넘김 버튼이 없습니다: {flow.labels()}")
    check(not flow.has("⭐ 대표 바꾸기"), "아무것도 고르지 않았는데 [⭐ 대표 바꾸기]가 보입니다.")

    # --- 쪽을 넘겨 뒤쪽 태그를 고른다 ---
    await flow.press("다음 ▶")
    check(flow.has("2/2"), f"둘째 쪽으로 넘어가지 않았습니다: {flow.labels()}")
    check(flow.has("#여분태그06"), f"둘째 쪽에 여분태그06이 없습니다: {flow.labels()}")

    await flow.press("#여분태그06")
    check("선택: ⭐여분태그06" in flow.text, f"선택·대표 표시가 없습니다: {flow.text!r}")
    check(not flow.has("#여분태그06"), "고른 태그가 버튼에 그대로 남아 있습니다.")
    check(flow.has("2/2"), f"태그를 고른 뒤 쪽 번호가 첫 쪽으로 튕겼습니다: {flow.labels()}")

    # --- 같은 쪽에서 하나 더 고른다 (선택 상태 유지 확인) ---
    await flow.press("#여분태그07")
    check("선택: ⭐여분태그06 · 여분태그07" in flow.text, f"둘째 태그가 안 붙었습니다: {flow.text!r}")
    check(flow.has("⭐ 대표 바꾸기"), "태그가 2개인데 [⭐ 대표 바꾸기]가 없습니다.")

    # --- 대표 바꾸기: 이미 고른 태그만 버튼으로 나온다 ---
    await flow.press("⭐ 대표 바꾸기")
    check(len(flow.labels()) == 2 + 2, f"고른 태그 2개만 나와야 합니다: {flow.labels()}")
    check(not flow.has("#공통매입세액"), "고르지 않은 태그가 대표 후보에 섞였습니다.")
    await flow.press("#여분태그07")
    check("선택: 여분태그06 · ⭐여분태그07" in flow.text, f"대표가 안 바뀌었습니다: {flow.text!r}")

    # --- 태그 빼기 ---
    await flow.press("🗑 태그 빼기")
    await flow.press("#여분태그06")
    check("선택: ⭐여분태그07" in flow.text, f"태그가 안 빠졌습니다: {flow.text!r}")
    await flow.press("⬅️ 뒤로")
    check(flow.has("#여분태그06"), f"뺀 태그가 버튼으로 돌아오지 않았습니다: {flow.labels()}")

    # --- 대표 태그가 제목이 되는지 ---
    await flow.press("✔️ 선택 완료")
    await flow.press("법령")
    await flow.press("⏭ 건너뛰기")
    await flow.press("⏭ 건너뛰기")
    await flow.press("⏭ 건너뛰기")
    await flow.press("⏭ 건너뛰기")
    await flow.say("대표 태그 확인용 본문")

    properties = last_properties()
    check(multi_of(properties, "개념태그") == ["여분태그07"], "개념태그가 다릅니다.")
    check(title_of(properties).startswith("여분태그07 · "),
          f"대표 태그가 제목에 쓰이지 않았습니다: {title_of(properties)!r}")


# ============================================================
# 3. 검색 → 새 태그 → 검색 상태 유지
# ============================================================
@case("검색 → [➕ 새 태그] → 검색 상태 유지 · 해제")
async def test_tag_search():
    flow = Flow()
    await walk_to_tag_step(flow)

    await flow.press("🔍 검색")
    await flow.say("매입")
    check("'매입' 로 걸러 보는 중" in flow.text, f"검색 중 표시가 없습니다: {flow.text!r}")
    check(flow.has("#공통매입세액") and flow.has("#예정신고기간매입"), f"검색 결과가 다릅니다: {flow.labels()}")
    check(not flow.has("#안분계산"), f"검색에 걸리지 않아야 할 태그가 있습니다: {flow.labels()}")

    # [➕ 새 태그]를 거쳐 돌아와도 검색 상태가 살아 있어야 한다 (명세서 §12-3).
    await flow.press("➕ 새 태그")
    await flow.say("매입세액불공제")
    check(flow.session["tag_query"] == "매입", "새 태그를 넣는 사이 검색어가 사라졌습니다.")
    check("'매입' 로 걸러 보는 중" in flow.text, f"검색 상태가 유지되지 않았습니다: {flow.text!r}")
    check("선택: ⭐매입세액불공제" in flow.text, f"새 태그가 선택되지 않았습니다: {flow.text!r}")
    check(not flow.has("#안분계산"), "새 태그를 넣자 검색이 풀렸습니다.")

    # 검색 해제
    await flow.press("🔎 검색 해제")
    check(flow.session["tag_query"] is None, "검색어가 해제되지 않았습니다.")
    check(flow.has("#안분계산"), f"검색을 풀었는데 전체 목록이 안 나옵니다: {flow.labels()}")


# ============================================================
# 4. 건너뛰기 4종
# ============================================================
@case("건너뛰기 4종 → 해당 속성이 빠짐")
async def test_skips():
    flow = Flow()
    await walk_to_tag_step(flow)
    await flow.press("#안분계산")
    await flow.press("✔️ 선택 완료")
    await flow.press("산식")
    for _ in range(4):  # 오답유형 · 교재구분 · 문제위치 · 참고출처
        await flow.press("⏭ 건너뛰기")
    await flow.say("건너뛰기 확인용 본문")

    properties = last_properties()
    left = [name for name in ("오답유형", "교재구분", "문제위치", "참고출처") if name in properties]
    check(not left, f"건너뛴 속성이 요청에 들어갔습니다: {left}")
    check(set(properties) == {"개념명", "과목", "단원", "개념태그", "지식유형"},
          f"속성 구성이 다릅니다: {sorted(properties)}")
    # 문제위치가 없으면 제목 뒤가 MM/DD가 된다 (명세서 §10-3).
    check(title_of(properties).startswith("안분계산 · "), f"개념명이 다릅니다: {title_of(properties)!r}")
    check(len(title_of(properties).split(" · ")[1]) == 5, f"MM/DD 형식이 아닙니다: {title_of(properties)!r}")


# ============================================================
# 5·6·7. 「같은 문제로 하나 더」
# ============================================================
async def first_record(flow):
    """교재구분·문제위치까지 채운 기록 한 건을 저장하고 저장 완료 화면에 선다."""
    await walk_to_tag_step(flow)
    await flow.press("#공통매입세액")
    await flow.press("✔️ 선택 완료")
    await flow.press("법령")
    await flow.press("⏭ 건너뛰기")
    await flow.press("연습서")
    await flow.say("연p.212#15")
    await flow.press("⏭ 건너뛰기")
    await flow.say("첫 기록 본문")


@case("[같은 문제로 하나 더] → 교재구분·문제위치 유지, 나머지 초기화")
async def test_again():
    flow = Flow()
    await first_record(flow)
    await flow.press("📌 같은 문제로 하나 더")

    data = flow.session["data"]
    check(data.get("교재구분") == "연습서", f"교재구분이 유지되지 않았습니다: {data.get('교재구분')!r}")
    check(data.get("문제위치") == "연p.212#15", f"문제위치가 유지되지 않았습니다: {data.get('문제위치')!r}")
    check(not data.get("개념태그"), "개념태그가 초기화되지 않았습니다.")
    check(not data.get("지식유형"), "지식유형이 초기화되지 않았습니다.")
    check(flow.session["step"] == bot.TAG_STEP, "개념태그 화면에서 다시 시작하지 않았습니다.")
    check("📌 유지 중" in flow.text, f"유지 중 안내가 없습니다: {flow.text!r}")

    # 교재구분·문제위치를 다시 묻지 않고 참고출처로 건너뛰어야 한다.
    await flow.press("#안분계산")
    await flow.press("✔️ 선택 완료")
    await flow.press("산식")
    await flow.press("⏭ 건너뛰기")
    check("참고 출처를 입력하세요" in flow.text, f"교재구분·문제위치를 다시 물었습니다: {flow.text!r}")
    await flow.press("⏭ 건너뛰기")
    await flow.say("둘째 기록 본문")

    check(len(SENT) == 2, f"기록이 2건이어야 하는데 {len(SENT)}건입니다.")
    properties = last_properties()
    check(select_of(properties, "교재구분") == "연습서", "둘째 기록의 교재구분이 다릅니다.")
    check(text_of(properties, "문제위치") == "연p.212#15", "둘째 기록의 문제위치가 다릅니다.")
    check(multi_of(properties, "개념태그") == ["안분계산"], "둘째 기록의 개념태그가 다릅니다.")
    check(title_of(properties) == "안분계산 · 연p.212#15", f"개념명이 다릅니다: {title_of(properties)!r}")


@case("[하나 더] 후 단원 이전까지 뒤로 → 유지 해제, 일반 흐름 복귀")
async def test_again_release():
    flow = Flow()
    await first_record(flow)
    await flow.press("📌 같은 문제로 하나 더")

    await flow.press("⬅️ 뒤로")   # 개념태그 → 단원
    check(flow.session["again"], "단원 화면에서 벌써 유지가 풀렸습니다.")
    await flow.press("⬅️ 뒤로")   # 단원 → 대분류

    session = flow.session
    check(not session["again"], "유지가 풀리지 않았습니다.")
    check(not session["locked"], "잠금이 풀리지 않았습니다.")
    check("교재구분" not in session["data"], "유지하던 교재구분이 남아 있습니다.")
    check("문제위치" not in session["data"], "유지하던 문제위치가 남아 있습니다.")
    check("해제했습니다" in flow.text, f"해제 안내가 없습니다: {flow.text!r}")

    # 일반 흐름으로 돌아와 교재구분을 다시 묻는지 본다.
    await flow.press("부가가치세")
    await flow.press("ch06.매입세액과 납부세액")
    await flow.press("#공통매입세액")
    await flow.press("✔️ 선택 완료")
    await flow.press("법령")
    await flow.press("⏭ 건너뛰기")
    check("교재구분을 선택해" in flow.text, f"교재구분을 다시 묻지 않습니다: {flow.text!r}")


@case("[✏️ 출처 바꾸기] → 원래 자리 복귀")
async def test_edit_source():
    flow = Flow()
    await first_record(flow)
    await flow.press("📌 같은 문제로 하나 더")
    await flow.press("#환급세액재계산")

    # 태그의 하위 화면에서는 [✏️ 출처 바꾸기]가 보이지 않아야 한다.
    # 잘못 누르면 입력하던 흐름이 끊긴다.
    check(flow.has("✏️ 출처 바꾸기"), f"태그 목록 화면에 [출처 바꾸기]가 없습니다: {flow.labels()}")
    for sub in ("➕ 새 태그", "🔍 검색", "🗑 태그 빼기"):
        await flow.press(sub)
        check(not flow.has("✏️ 출처 바꾸기"),
              f"[{sub}] 하위 화면에 [출처 바꾸기]가 보입니다: {flow.labels()}")
        await flow.press("⬅️ 뒤로")
    check(flow.has("✏️ 출처 바꾸기"), f"하위 화면에서 나온 뒤 [출처 바꾸기]가 사라졌습니다: {flow.labels()}")

    await flow.press("✏️ 출처 바꾸기")
    check("교재구분을 선택해" in flow.text, f"교재구분 화면으로 가지 않았습니다: {flow.text!r}")
    await flow.press("기출")
    await flow.say("기p.99#07")

    session = flow.session
    check(session["step"] == bot.TAG_STEP, f"원래 자리(개념태그)로 돌아오지 않았습니다: step={session['step']}")
    check(session["data"]["문제위치"] == "기p.99#07", "고친 문제위치가 반영되지 않았습니다.")
    check(session["resume"] is None, "resume이 정리되지 않았습니다.")
    check(session["locked"] == set(bot.KEEP_STEPS), "고친 출처를 다시 묻지 않도록 잠기지 않았습니다.")
    check("선택: ⭐환급세액재계산" in flow.text, f"고른 태그가 사라졌습니다: {flow.text!r}")

    # 출처 수정 중 [⬅️ 뒤로] → 저장 직전에 채운 단계를 다시 묻지 않아야 한다 (§3-2).
    flow2 = Flow()
    await first_record(flow2)
    await flow2.press("📌 같은 문제로 하나 더")
    await flow2.press("#환급세액재계산")
    await flow2.press("✔️ 선택 완료")
    await flow2.press("법령")            # 지식유형까지 채운 뒤
    await flow2.press("✏️ 출처 바꾸기")
    await flow2.press("⬅️ 뒤로")          # 출처 수정을 그만둔다
    check(flow2.session["resume"] is None, "뒤로 간 뒤에도 resume이 남아 있습니다.")

    await flow2.press("⏭ 건너뛰기")       # 오답유형
    await flow2.press("연습서")
    await flow2.say("연p.212#15")
    await flow2.press("⏭ 건너뛰기")       # 참고출처
    check("정리 내용을 입력" in flow2.text,
          f"이미 채운 단계를 다시 물었습니다: {flow2.text!r}")
    await flow2.say("뒤로 확인용 본문")
    check("✅ 저장 완료" in flow2.text, f"저장까지 가지 못했습니다: {flow2.text!r}")


# ============================================================
# 8. 필수값 누락
# ============================================================
@case("필수값 누락 → KeyError가 아니라 한국어 안내")
async def test_missing_required():
    flow = Flow()
    await walk_to_tag_step(flow)
    await flow.press("#공통매입세액")
    await flow.press("✔️ 선택 완료")
    await flow.press("법령")
    await flow.press("⏭ 건너뛰기")
    await flow.press("⏭ 건너뛰기")
    await flow.press("⏭ 건너뛰기")
    await flow.press("⏭ 건너뛰기")

    # 어떤 경로로든 값이 비어 버린 상황을 만든다.
    flow.session["data"].pop("지식유형")
    await flow.say("필수값 누락 확인용 본문")

    check(not SENT, "필수값이 비었는데도 노션으로 보냈습니다.")
    check("지식유형" in flow.text and "비어 있어 저장할 수 없습니다" in flow.text,
          f"한국어 안내가 나오지 않았습니다: {flow.text!r}")
    check(flow.session["step"] == bot.STEPS.index("knowledge"),
          "비어 있는 값을 입력하는 단계로 되돌아가지 않았습니다.")

    # 되돌아간 자리에서 다시 채우면, 그 단계부터 이어서 끝까지 갈 수 있다.
    await flow.press("암기")
    check("오답유형" in flow.text, f"되돌아간 단계에서 흐름이 이어지지 않았습니다: {flow.text!r}")
    for _ in range(4):
        await flow.press("⏭ 건너뛰기")
    await flow.say("다시 채운 뒤의 본문")
    check("✅ 저장 완료" in flow.text, f"다시 채운 뒤에도 저장되지 않았습니다: {flow.text!r}")
    check(select_of(last_properties(), "지식유형") == "암기", "다시 고른 지식유형이 저장되지 않았습니다.")


# ============================================================
# 9. 백업 복구 (손상된 항목이 섞여 있어도 나머지는 계속)
# ============================================================
@case("손상된 백업 항목이 있어도 나머지 복구가 계속됨")
async def test_retry_pending():
    good = bot.build_item({
        "과목": "세법",
        "대분류": "부가가치세",
        "단원": "ch06.매입세액과 납부세액",
        "개념태그": ["공통매입세액"],
        "지식유형": "법령",
        "문제위치": "TEST_복구확인",
        "정리 내용": "복구 확인용 본문",
    }, None)
    broken_empty = {"id": "broken-1", "시각": "망가짐", "제목": "속성이 빈 기록", "속성": {}}
    broken_shape = {"id": "broken-2", "시각": "망가짐", "제목": "속성이 없는 기록", "속성": "문자열"}

    bot.write_pending([broken_empty, good, broken_shape])
    report = bot.retry_pending()

    check("✅ 저장했습니다" in report, f"멀쩡한 항목이 저장되지 않았습니다:\n{report}")
    check(report.count("❌") == 2, f"손상된 항목 2건이 걸러지지 않았습니다:\n{report}")
    check("필수 속성이 비어 있습니다" in report, f"빈 속성 안내가 없습니다:\n{report}")
    check("백업 파일이 손상된 것 같습니다" in report, f"모양이 어긋난 항목 안내가 없습니다:\n{report}")
    for english in ("Traceback", "KeyError", "Error:"):
        check(english not in report, f"영어 오류가 화면에 새어 나왔습니다 ({english}):\n{report}")

    check(len(SENT) == 1 and SENT[0]["제목"] == good["제목"],
          "멀쩡한 항목이 노션으로 가지 않았습니다.")

    left, _ = bot.read_pending()
    check({item["id"] for item in left} == {"broken-1", "broken-2"},
          f"실패한 항목만 백업에 남아야 합니다: {[item['id'] for item in left]}")


# ============================================================
# 10. 백업 파일의 옛 속성 이름
# ============================================================
@case("옛 속성 이름으로 저장된 백업 항목도 읽힘")
async def test_old_keys():
    old_item = {
        "id": "old-1",
        "시각": "2026-09-09T10:00:00",
        "제목": "옛 이름 기록",
        "속성": {
            "개념명": {"title": [{"type": "text", "text": {"content": "옛 이름 기록"}}]},
            "과목": {"select": {"name": "세법"}},
            "단원": {"select": {"name": "부가가치세-ch06.매입세액과 납부세액"}},
            "개념태그": {"multi_select": [{"name": "공통매입세액"}]},
            "지식유형": {"select": {"name": "법령"}},
            "출처구분": {"select": {"name": "연습서"}},
            "문제출처": {"rich_text": [{"type": "text", "text": {"content": "TEST_옛이름"}}]},
            "근거": {"rich_text": [{"type": "text", "text": {"content": "워p.131"}}]},
        },
        "정리 내용": "옛 이름으로 저장된 백업입니다.",
    }
    bot.write_pending([old_item])
    report = bot.retry_pending()

    check("✅ 저장했습니다" in report, f"옛 이름 항목을 복구하지 못했습니다:\n{report}")
    properties = last_properties()
    for old, new in bot.RENAMED_PROPERTIES.items():
        check(old not in properties, f"옛 이름 `{old}`이 그대로 보내졌습니다.")
        check(new in properties, f"새 이름 `{new}`으로 바뀌지 않았습니다.")
    check(select_of(properties, "교재구분") == "연습서", "교재구분 값이 옮겨지지 않았습니다.")
    check(text_of(properties, "문제위치") == "TEST_옛이름", "문제위치 값이 옮겨지지 않았습니다.")
    check(text_of(properties, "참고출처") == "워p.131", "참고출처 값이 옮겨지지 않았습니다.")


# ============================================================
# 11. 정리 내용 서식 파서 (명세서 §13-1)
# ============================================================
# 명세서 §13-1의 6가지 변환을 한 번에 지나가는 원문. 실제로 쓰일 법한
# 가중평균자본비용(WACC) 정리를 그 규격대로 적었다.
WACC_SOURCE = """## 가중평균자본비용(WACC)

기업이 조달한 자본 전체의 평균 비용이다.

$$
WACC = \\frac{E}{V} r_e + \\frac{D}{V} r_d (1 - t)
$$

- E: 자기자본의 시장가치
- D: 타인자본의 시장가치
  - 이자부 부채만 포함한다
  - 매입채무는 제외한다
- V = E + D

1. 자본구조 비율을 구한다
2. 세후 타인자본비용을 구한다
3. 가중평균한다

:::code text
WACC = 0.6 * 0.12 + 0.4 * 0.05 * (1 - 0.22)
     = 0.0876
:::

법인세 절감효과는 타인자본비용에만 반영한다."""


def body_of(item):
    """저장된 기록의 본문을 노션 블록 목록으로 바꾼다 (bot이 실제로 보내는 것과 같은 경로)."""
    return bot.body_blocks(item["정리 내용"])


def types_of(blocks):
    return [block["type"] for block in blocks]


def content_of(block):
    return content_format.plain_text_of(block)


@case("서식 파서 — §13-1의 6가지 변환이 모두 동작")
async def test_format_parser():
    flow = Flow()
    await walk_to_tag_step(flow)
    await flow.press("#공통매입세액")
    await flow.press("✔️ 선택 완료")
    await flow.press("산식")
    for _ in range(4):
        await flow.press("⏭ 건너뛰기")
    await flow.say(WACC_SOURCE)

    check("✅ 저장 완료" in flow.text, f"저장되지 않았습니다: {flow.text!r}")
    blocks = body_of(SENT[-1])
    kinds = types_of(blocks)

    check(kinds == [
        "heading_2", "paragraph", "equation",
        "bulleted_list_item", "bulleted_list_item", "bulleted_list_item",
        "numbered_list_item", "numbered_list_item", "numbered_list_item",
        "code", "paragraph",
    ], f"블록 구성이 다릅니다: {kinds}")

    check(content_of(blocks[0]) == "가중평균자본비용(WACC)", f"heading_2 내용이 다릅니다: {content_of(blocks[0])!r}")
    check("\\frac{E}{V}" in blocks[2]["equation"]["expression"], "equation에 수식이 들어가지 않았습니다.")
    check("$$" not in blocks[2]["equation"]["expression"], "equation에 구분자가 남아 있습니다.")

    # `- ` 목록의 공백 2칸 들여쓰기가 children으로 중첩되었는가
    children = blocks[4]["bulleted_list_item"].get("children") or []
    check(len(children) == 2, f"중첩 목록이 children으로 들어가지 않았습니다: {children}")
    check(types_of(children) == ["bulleted_list_item", "bulleted_list_item"], "중첩 항목의 종류가 다릅니다.")
    check(content_of(children[0]) == "이자부 부채만 포함한다", f"중첩 내용이 다릅니다: {content_of(children[0])!r}")

    # 코드 블록: 언어가 노션 이름으로 바뀌고 본문이 그대로 들어가는가
    check(blocks[9]["code"]["language"] == "plain text",
          f"코드 언어가 다릅니다: {blocks[9]['code']['language']!r}")
    check("0.0876" in content_of(blocks[9]), "코드 본문이 다릅니다.")
    check(":::" not in content_of(blocks[9]), "코드 본문에 구분자가 남아 있습니다.")

    # 빈 줄은 구분자로만 쓰고 빈 paragraph를 만들지 않는다
    empty = [b for b in blocks if b["type"] == "paragraph" and content_of(b) == ""]
    check(not empty, f"빈 paragraph가 {len(empty)}개 생겼습니다.")

    # mermaid도 허용된다
    mermaid = bot.body_blocks(":::code mermaid\ngraph TD; A-->B;\n:::")
    check(types_of(mermaid) == ["code"] and mermaid[0]["code"]["language"] == "mermaid",
          f"mermaid 코드블록이 만들어지지 않았습니다: {mermaid}")

    # 한 줄짜리 `$$ ... $$` 형태도 처리한다
    one_line = bot.body_blocks("$$ E = mc^2 $$")
    check(types_of(one_line) == ["equation"] and one_line[0]["equation"]["expression"] == "E = mc^2",
          f"한 줄 수식이 처리되지 않았습니다: {one_line}")


# ============================================================
# 12. 서식이 없는 평범한 글 (회귀)
# ============================================================
@case("서식 없는 평범한 글이 전과 같이 문단으로 저장됨 (회귀)")
async def test_plain_regression():
    plain = (
        "공통매입세액은 과세·면세 공급가액 비율로 안분한다.\n"
        "실지귀속을 구분할 수 없을 때만 쓴다.\n"
        "예정신고기간에는 예정분 비율을 쓰고 확정신고 때 정산한다."
    )
    flow = Flow()
    await walk_to_tag_step(flow)
    await flow.press("#안분계산")
    await flow.press("✔️ 선택 완료")
    await flow.press("법령")
    for _ in range(4):
        await flow.press("⏭ 건너뛰기")
    await flow.say(plain)

    check("✅ 저장 완료" in flow.text, f"저장되지 않았습니다: {flow.text!r}")
    blocks = body_of(SENT[-1])
    check(types_of(blocks) == ["paragraph"] * 3, f"문단 3개가 아닙니다: {types_of(blocks)}")
    check([content_of(b) for b in blocks] == plain.split("\n"), "문단 내용이 원문과 다릅니다.")

    # 2000자를 넘는 한 줄은 문단 하나 안에서 rich_text만 나뉜다 (원문에 없던 줄바꿈 금지)
    long_line = "가" * 4500
    long_blocks = bot.body_blocks(long_line)
    check(types_of(long_blocks) == ["paragraph"], f"긴 줄이 여러 문단으로 쪼개졌습니다: {types_of(long_blocks)}")
    pieces = long_blocks[0]["paragraph"]["rich_text"]
    check(len(pieces) == 3, f"rich_text 덩어리가 3개여야 합니다: {len(pieces)}")
    check(all(len(p["text"]["content"]) <= 2000 for p in pieces), "2000자를 넘는 덩어리가 있습니다.")
    check(content_of(long_blocks[0]) == long_line, "긴 줄의 내용이 바뀌었습니다.")

    # 블록 100개 제한: 앞 100개로 페이지를 만들고 나머지는 이어붙인다
    many = bot.body_blocks("\n".join(f"{i}번째 줄" for i in range(250)))
    check(len(many) == 250, f"줄 수가 다릅니다: {len(many)}")
    payload, rest = bot.page_payload({"개념명": {}}, many)
    check(len(payload["children"]) == bot.BLOCK_LIMIT, "페이지 생성 요청이 100개를 넘었습니다.")
    check(len(rest) == 150, f"뒤로 미룬 블록 수가 다릅니다: {len(rest)}")

    # 서식이 섞여 블록 종류가 늘어도 100개 분할은 그대로 맞는다
    mixed = bot.body_blocks("\n\n".join(
        ["## 제목", "$$\nx = 1\n$$", "- 항목", "1. 번호", ":::code text\ncode\n:::", "문단"] * 30
    ))
    payload, rest = bot.page_payload({"개념명": {}}, mixed)
    check(len(payload["children"]) == bot.BLOCK_LIMIT, "서식이 섞였을 때 100개 분할이 깨집니다.")
    check(len(payload["children"]) + len(rest) == len(mixed), "분할에서 블록이 새거나 늘었습니다.")


# ============================================================
# 13. 손상 감지 안전망 (명세서 §15-2)
# ============================================================
async def content_step(flow, tag="#환급세액재계산"):
    """정리 내용 입력 화면까지 간다."""
    await walk_to_tag_step(flow)
    await flow.press(tag)
    await flow.press("✔️ 선택 완료")
    await flow.press("법령")
    for _ in range(4):
        await flow.press("⏭ 건너뛰기")
    check("정리 내용을 입력" in flow.text, f"정리 내용 화면이 아닙니다: {flow.text!r}")


@case("손상 감지 — 정황 3가지를 잡고, 정상 원문에는 오탐 없음")
async def test_damage_detection():
    broken = [
        ("수식 구분자 소실", "감가상각비는 \\frac{취득원가 - 잔존가치}{내용연수} 로 구한다."),
        ("코드블록 구분자 소실", ":::code text\nWACC = 0.0876\n계산 과정은 위와 같다."),
        ("수식 구분자 일부 소실", "$$\nWACC = w_e r_e + w_d r_d\n비율은 시가 기준이다."),
    ]
    for label, text in broken:
        check(content_format.detect_corruption(text) is not None, f"[{label}]을 잡지 못했습니다.")

    # 정상 원문에는 뜨지 않아야 한다
    for label, text in [
        ("WACC 규격 원문", WACC_SOURCE),
        ("서식 없는 평범한 글", "공통매입세액은 과세·면세 공급가액 비율로 안분한다."),
        ("한 줄 수식", "정리하면 $$ E = mc^2 $$ 이다."),
        ("코드블록만", ":::code text\nprint(1)\n:::"),
    ]:
        reason = content_format.detect_corruption(text)
        check(reason is None, f"정상 원문 [{label}]에 오탐이 났습니다: {reason}")

    # --- 화면 흐름: 경고 → [🔁 그래도 저장] ---
    flow = Flow()
    await content_step(flow)
    await flow.say(broken[0][1])
    check("서식이 깨진 것 같습니다" in flow.text, f"경고 화면이 아닙니다: {flow.text!r}")
    check(not SENT, "경고를 띄우고도 노션으로 보냈습니다.")
    check(flow.session["data"]["정리 내용"] == broken[0][1], "입력 내용이 세션에 보존되지 않았습니다.")

    await flow.press("🔁 그래도 저장")
    check("✅ 저장 완료" in flow.text, f"강행 저장이 되지 않았습니다: {flow.text!r}")
    check(len(SENT) == 1, f"기록이 1건이어야 하는데 {len(SENT)}건입니다.")
    check(SENT[-1]["정리 내용"] == broken[0][1], "강행 저장에서 입력 내용이 바뀌었습니다.")

    # --- 화면 흐름: 경고 → [❌ 취소] → 다시 입력 ---
    flow2 = Flow()
    await content_step(flow2)
    await flow2.say(broken[1][1])
    check("서식이 깨진 것 같습니다" in flow2.text, f"경고 화면이 아닙니다: {flow2.text!r}")
    await flow2.press("❌ 취소")
    check("저장하지 않았습니다" in flow2.text, f"취소 안내가 없습니다: {flow2.text!r}")
    check(len(SENT) == 1, "취소했는데 노션으로 보냈습니다.")
    check(flow2.session["step"] == bot.STEPS.index("content"), "정리 내용 단계로 돌아오지 않았습니다.")

    await flow2.say(":::code text\nWACC = 0.0876\n:::")
    check("✅ 저장 완료" in flow2.text, f"다시 보낸 뒤 저장되지 않았습니다: {flow2.text!r}")
    check(len(SENT) == 2, f"기록이 2건이어야 하는데 {len(SENT)}건입니다.")

    # 정상 원문은 경고 없이 바로 저장된다
    flow3 = Flow()
    await content_step(flow3)
    await flow3.say(WACC_SOURCE)
    check("✅ 저장 완료" in flow3.text, f"정상 원문이 경고에 걸렸습니다: {flow3.text!r}")


# ============================================================
# 14. 왕복 — 원문 → 블록 → 마크다운
# ============================================================
@case("왕복 — 원문 → 노션 블록 → 마크다운 복원이 원문과 일치")
async def test_round_trip():
    blocks = bot.body_blocks(WACC_SOURCE)
    restored = content_format.blocks_to_markdown(blocks)
    check(restored == WACC_SOURCE,
          "복원한 마크다운이 원문과 다릅니다.\n"
          f"      --- 원문 ---\n{WACC_SOURCE}\n"
          f"      --- 복원 ---\n{restored}")

    # 복원한 글을 다시 붙여넣어도 같은 블록이 나와야 한다 (명세서 §16-4)
    check(bot.body_blocks(restored) == blocks, "복원한 글을 다시 저장하면 다른 블록이 됩니다.")

    # 노션이 돌려주는 모양(plain_text)으로도 같은 결과가 나와야 한다.
    # /export는 우리가 만든 블록이 아니라 노션이 읽어 준 블록을 복원하기 때문이다.
    def as_notion(items):
        out = []
        for block in items:
            copied = copy.deepcopy(block)
            payload = copied[copied["type"]]
            for piece in payload.get("rich_text", []):
                piece["plain_text"] = piece.pop("text")["content"]
            if payload.get("children"):
                payload["children"] = as_notion(payload["children"])
            out.append(copied)
        return out

    check(content_format.blocks_to_markdown(as_notion(blocks)) == WACC_SOURCE,
          "노션이 돌려준 모양의 블록에서 복원이 어긋납니다.")

    # /export 파일 한 기록의 머리말 + 본문 모양 (명세서 §16-3)
    page = {
        "created_time": "2026-09-12T01:00:00.000Z",
        "properties": {
            "개념명": {"type": "title", "title": [{"plain_text": "가중평균자본비용 · 연p.212#15"}]},
            "과목": {"type": "select", "select": {"name": "재무"}},
            "단원": {"type": "select", "select": {"name": "자본비용-ch03.자본비용"}},
            "개념태그": {"type": "multi_select", "multi_select": [{"name": "WACC"}]},
            "지식유형": {"type": "select", "select": {"name": "산식"}},
            "오답유형": {"type": "multi_select", "multi_select": []},
            "교재구분": {"type": "select", "select": {"name": "연습서"}},
            "문제위치": {"type": "rich_text", "rich_text": [{"plain_text": "연p.212#15"}]},
            "참고출처": {"type": "rich_text", "rich_text": []},
            "기록일": {"type": "created_time", "created_time": "2026-09-12T01:00:00.000Z"},
        },
    }
    record = bot.record_markdown(page, as_notion(blocks))
    check(record.startswith("## 가중평균자본비용 · 연p.212#15\n"), f"머리말이 다릅니다:\n{record[:200]}")
    check("- 재무 / 자본비용-ch03.자본비용 / WACC / 산식\n" in record, f"속성 줄이 다릅니다:\n{record[:300]}")
    check("- 연습서 · 연p.212#15 / 2026-09-12\n" in record, f"출처 줄이 다릅니다:\n{record[:300]}")
    check(record.endswith(WACC_SOURCE), "본문이 원문 그대로 복원되지 않았습니다.")

    # 파일 전체를 만들고, 그 안의 본문을 다시 파서에 넣어도 같은 블록이 나온다
    whole = bot.build_export_file({"과목": "재무"}, [record])
    check("---" in whole and whole.startswith("# 재무-전체"), f"파일 머리말이 다릅니다:\n{whole[:200]}")
    # 파일 안의 기록 하나를 꺼내, 머리말(제목 줄 + 속성 줄들)을 떼어 낸 본문
    exported = whole.split("\n\n---\n\n")[1].rstrip("\n")
    body = exported.split("\n\n", 1)[1]
    check(bot.body_blocks(body) == blocks, "내보낸 파일의 본문을 다시 저장하면 다른 블록이 됩니다.")

    # 텔레그램 파일 크기 제한을 넘으면 기록 단위로 나눈다
    saved = bot.TELEGRAM_FILE_LIMIT
    try:
        bot.TELEGRAM_FILE_LIMIT = 1500
        files = bot.split_export({"과목": "재무"}, [record] * 5)
        check(len(files) > 1, f"제한을 넘었는데 나누지 않았습니다: {len(files)}개")
        check(all(len(text.encode('utf-8')) <= 1500 or len(files) == 1 for _, text in files),
              "나눈 뒤에도 제한을 넘는 파일이 있습니다.")
        check(all(name.endswith(".md") for name, _ in files), "파일 이름이 .md로 끝나지 않습니다.")
        check(len({name for name, _ in files}) == len(files), "파일 이름이 겹칩니다.")
    finally:
        bot.TELEGRAM_FILE_LIMIT = saved

    # 파일 이름에 범위와 날짜가 드러나는가
    name = bot.export_filename({"과목": "세법", "단원": "부가가치세-ch06.매입세액과 납부세액"})
    check("세법" in name and "ch06" in name, f"파일 이름에 범위가 없습니다: {name}")
    check(datetime.now().strftime("%Y%m%d") in name, f"파일 이름에 날짜가 없습니다: {name}")
    check("/" not in name and " " not in name, f"파일 이름에 쓸 수 없는 글자가 있습니다: {name}")


# ============================================================
# 15. 검색 결과가 10개를 넘을 때의 쪽 넘김
# ============================================================
@case("검색 결과 10개 초과 → 쪽 넘김이 걸러진 목록에만 적용됨")
async def test_search_paging():
    flow = Flow()
    await walk_to_tag_step(flow)

    matched = [name for name, _ in CHAPTER_TAGS if "여분태그" in name]
    check(len(matched) > bot.PAGE_SIZE,
          f"검사 전제가 깨졌습니다. 여분태그가 {len(matched)}개뿐입니다.")

    await flow.press("🔍 검색")
    await flow.say("여분태그")
    check("'여분태그' 로 걸러 보는 중" in flow.text, f"검색 중 표시가 없습니다: {flow.text!r}")

    pages = (len(matched) + bot.PAGE_SIZE - 1) // bot.PAGE_SIZE
    check(flow.has(f"1/{pages}"), f"검색 결과에 쪽 넘김이 없습니다: {flow.labels()}")
    check(not flow.has("#공통매입세액"), f"검색에 걸리지 않는 태그가 섞였습니다: {flow.labels()}")
    check(sum(1 for one in flow.labels() if one.startswith("#")) == bot.PAGE_SIZE,
          f"한 쪽에 {bot.PAGE_SIZE}개가 나와야 합니다: {flow.labels()}")

    # 둘째 쪽에도 걸러진 목록만 나온다
    await flow.press("다음 ▶")
    check(flow.has(f"2/{pages}"), f"둘째 쪽으로 넘어가지 않았습니다: {flow.labels()}")
    second = [one for one in flow.labels() if one.startswith("#")]
    check(len(second) == len(matched) - bot.PAGE_SIZE, f"둘째 쪽 개수가 다릅니다: {second}")
    check(all("여분태그" in one for one in second), f"둘째 쪽에 다른 태그가 섞였습니다: {second}")

    # 둘째 쪽에서 고른 뒤에도 검색과 쪽이 유지된다
    await flow.press(second[0])
    check(flow.session["tag_query"] == "여분태그", "태그를 고르자 검색이 풀렸습니다.")
    check("선택: ⭐여분태그" in flow.text, f"태그가 선택되지 않았습니다: {flow.text!r}")

    # 검색을 풀면 전체 목록의 쪽 넘김으로 돌아온다
    await flow.press("🔎 검색 해제")
    everything = len(CHAPTER_TAGS) - 1  # 방금 고른 태그 하나는 버튼에서 빠진다
    check(flow.has(f"/{(everything + bot.PAGE_SIZE - 1) // bot.PAGE_SIZE}"),
          f"전체 목록의 쪽 넘김이 아닙니다: {flow.labels()}")
    check(flow.has("#공통매입세액"), f"검색을 풀었는데 전체 목록이 안 나옵니다: {flow.labels()}")


# ============================================================
# 16. iOS 손상 미감지의 재현과 수정 (명세서 §15-3)
# ============================================================
# §13-5 입력 규격을 그대로 지킨 원문. §14-1이 실측한 원본과 같은 성격이다
# (`$$` 2개, `##` 2개, 줄머리 `- ` 4개, `:::` 2개, `\text{}`·`\%` 사용).
IOS_SOURCE = """## 공통매입세액 안분계산

- 과세사업과 면세사업에 공통으로 쓰인 매입세액은 안분한다.
- 안분 기준은 직전 과세기간의 공급가액 비율이다.

$$
\\text{안분율} = \\frac{\\text{과세공급가액}}{\\text{총공급가액}} \\times 100\\%
$$

## 적용 배제

- 공통매입세액이 5백만원 미만 - 전액 공제
- 면세비율이 5\\% 미만 - 전액 공제

:::code text
공제세액 = 공통매입세액 - 불공제액
:::"""


def ios_mangle(text, strip_backslash=False, em_dash=False):
    """§14-1 실측대로 iOS "서식 없이 보내기"가 하는 짓을 그대로 흉내 낸다.

    `$$` `##` 줄머리 `- ` 는 지워지고, `_` `:::` 는 남는다.
    strip_backslash·em_dash는 §15-3이 남겨 둔 두 물음(역슬래시가 지워지는가,
    하이픈이 긴 대시가 되는가)에 대한 두 갈래다.
    """
    out = []
    for line in text.split("\n"):
        if line.startswith("## "):
            line = line[3:]
        if line.strip() == "$$":
            continue
        stripped = line.lstrip()
        if stripped.startswith("- "):
            line = line[:len(line) - len(stripped)] + stripped[2:]
        out.append(line)
    text = "\n".join(out)
    if strip_backslash:
        text = text.replace("\\", "")
    if em_dash:
        text = text.replace(" - ", " \u2014 ")
    return text


@case("iOS 손상 재현 — 역슬래시가 지워진 글도 감지에 걸린다 (§15-3)")
async def test_ios_detection():
    # 재구성한 원문 자체는 §14-1의 실측 개수와 맞아야 한다
    check(IOS_SOURCE.count("$$") == 2, "재구성 원문의 `$$` 개수가 실측과 다릅니다.")
    check(IOS_SOURCE.count("##") == 2, "재구성 원문의 `##` 개수가 실측과 다릅니다.")
    check(IOS_SOURCE.count(":::") == 2, "재구성 원문의 `:::` 개수가 실측과 다릅니다.")
    check(content_format.detect_corruption(IOS_SOURCE) is None,
          "규격을 지킨 원문이 손상으로 오판되었습니다.")

    # iOS가 보낸 세 갈래 모두 걸려야 한다. 예전 코드는 아래 두 번째·세 번째를
    # 놓쳤다 — `\frac`의 역슬래시가 지워지면 LaTeX 명령어로 보이지 않았기 때문이다.
    for label, mangled in [
        ("역슬래시 잔존", ios_mangle(IOS_SOURCE)),
        ("역슬래시 소실", ios_mangle(IOS_SOURCE, strip_backslash=True)),
        ("역슬래시 소실 + 긴 대시", ios_mangle(IOS_SOURCE, strip_backslash=True, em_dash=True)),
    ]:
        check(mangled.count("$$") == 0, f"[{label}] 재현이 잘못되었습니다: `$$`가 남아 있습니다.")
        check(mangled.count(":::") == 2, f"[{label}] 재현이 잘못되었습니다: `:::`가 사라졌습니다.")
        check(content_format.detect_corruption(mangled) is not None,
              f"iOS 손상 [{label}]을 잡지 못했습니다.")

    # 코드블록 안은 LaTeX 검사에서 빠진다 — 기존 규칙의 범위 축소다.
    for label, text in [
        ("윈도우 경로", ":::code text\nC:\\Users\\kan\\Desktop\\cpa\n:::"),
        ("중괄호가 든 코드", ":::code text\nint main() {\n  return 0;\n}\n:::"),
        ("긴 대시가 쓰인 산문", "이연법인세 \u2014 장부금액과 세무기준액의 차이에서 생긴다."),
        ("영문 약어 산문", "WACC는 자기자본비용과 타인자본비용의 가중평균이다."),
        ("괄호 쓴 산문", "감가상각(정액법)은 취득원가에서 잔존가치를 뺀 값을 나눈다."),
    ]:
        reason = content_format.detect_corruption(text)
        check(reason is None, f"정상 원문 [{label}]에 오탐이 났습니다: {reason}")

    # 화면 흐름 — iOS 글을 보내면 경고가 뜨고, 강행하면 내용이 그대로 저장된다
    mangled = ios_mangle(IOS_SOURCE, strip_backslash=True)
    flow = Flow()
    await content_step(flow)
    await flow.say(mangled)
    check("서식이 깨진 것 같습니다" in flow.text, f"경고 화면이 아닙니다: {flow.text!r}")
    check(not SENT, "경고를 띄우고도 노션으로 보냈습니다.")

    await flow.press("🔁 그래도 저장")
    check("✅ 저장 완료" in flow.text, f"강행 저장이 되지 않았습니다: {flow.text!r}")
    check(SENT[-1]["정리 내용"] == mangled, "강행 저장에서 입력 내용이 바뀌었습니다.")


# ============================================================
# 17. /export 두 선택지와 추출본 표 저장 (명세서 §16-5 · §16-6 · §17)
# ============================================================
EXPORT_PAGE = {
    "id": "page-1",
    "created_time": "2026-09-12T01:00:00.000Z",
    "properties": {
        "개념명": {"type": "title", "title": [{"plain_text": "공통매입세액 · 연p.212#15"}]},
        "과목": {"type": "select", "select": {"name": "세법"}},
        "단원": {"type": "select", "select": {"name": "부가가치세-ch06.매입세액과 납부세액"}},
        "개념태그": {"type": "multi_select", "multi_select": [{"name": "공통매입세액"}]},
        "지식유형": {"type": "select", "select": {"name": "법령"}},
        "오답유형": {"type": "multi_select", "multi_select": []},
        "교재구분": {"type": "select", "select": {"name": "연습서"}},
        "문제위치": {"type": "rich_text", "rich_text": [{"plain_text": "연p.212#15"}]},
        "참고출처": {"type": "rich_text", "rich_text": []},
        "기록일": {"type": "created_time", "created_time": "2026-09-12T01:00:00.000Z"},
    },
}

EXPORT_BLOCKS = [
    {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "공통매입세액은 안분한다."}]}},
]


class FakeResponse:
    """노션이 돌려준 응답인 척하는 것. ok·status_code·json()만 있으면 된다."""

    ok = True
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def install_export_fakes(pages):
    """/export가 부르는 노션 함수를 바꿔치고, 보낸 요청을 적어 둘 목록을 준다."""
    posted = []
    patched = []

    def fake_query_pages(filter_payload):
        return list(pages), None

    def fake_fetch_blocks(block_id):
        return copy.deepcopy(EXPORT_BLOCKS), None

    def fake_ensure_export_db():
        return "TEST_EXPORT_DATA_SOURCE", None

    def fake_notion_post(path, token, payload):
        posted.append((path, payload))
        return FakeResponse({"id": "TEST_EXPORT_PAGE", "url": "https://notion.example/TEST_EXPORT"})

    def fake_notion_patch(path, token, payload):
        patched.append((path, payload))
        return FakeResponse({})

    bot.query_pages = fake_query_pages
    bot.fetch_blocks = fake_fetch_blocks
    bot.ensure_export_db = fake_ensure_export_db
    bot.notion_post = fake_notion_post
    bot.notion_patch = fake_notion_patch
    return posted, patched


async def walk_to_deliver(flow):
    """/export → 세법 → 과목 전체 까지 가서 두 선택지 화면에 선다."""
    await flow.command(bot.cmd_export)
    await flow.press("세법")
    await flow.press("📚 과목 전체")


@case("/export 수집 후 두 선택지 → [📄 노션에 저장]이 추출본 표에 페이지를 만든다")
async def test_export_to_notion():
    posted, patched = install_export_fakes([EXPORT_PAGE] * 3)

    flow = Flow()
    await walk_to_deliver(flow)

    # 명세서 §16-5 — 파일과 노션 두 선택지가 나온다
    check("3건을 모았습니다" in flow.text, f"수집 결과 화면이 아닙니다: {flow.text!r}")
    check(flow.has("📥 파일로 받기"), f"[파일로 받기] 버튼이 없습니다: {flow.labels()}")
    check(flow.has("📄 노션에 저장"), f"[노션에 저장] 버튼이 없습니다: {flow.labels()}")
    check(not flow.documents, "선택하기도 전에 파일을 보냈습니다.")

    await flow.press("📄 노션에 저장")

    check(len(posted) == 1, f"페이지 생성 요청이 1건이어야 하는데 {len(posted)}건입니다.")
    path, payload = posted[0]
    check(path == "/pages", f"엉뚱한 곳에 요청했습니다: {path}")

    # 학습 기록 표와 절대 섞이면 안 된다 (명세서 §17-1)
    parent = payload["parent"]["data_source_id"]
    check(parent == "TEST_EXPORT_DATA_SOURCE", f"추출본 표가 아닌 곳에 저장했습니다: {parent}")
    check(parent != bot.DATA_SOURCE_ID, "추출본이 학습 기록 표에 저장되었습니다.")

    # 속성 5개 (§17-2). `생성일`은 노션이 자동으로 넣으므로 보내지 않는다.
    properties = payload["properties"]
    check(set(properties) == {"제목", "과목", "범위", "포함건수"},
          f"추출본 속성이 명세와 다릅니다: {sorted(properties)}")
    title = "".join(piece["text"]["content"] for piece in properties["제목"]["title"])
    expected = f"세법 › 전체 · {datetime.now().strftime('%Y-%m-%d')}"
    check(title == expected, f"제목 형식이 다릅니다: {title!r} (기대: {expected!r})")
    check(properties["과목"]["select"]["name"] == "세법", "과목이 다릅니다.")
    check(text_of(properties, "범위") == "전체", "범위가 다릅니다.")
    check(properties["포함건수"]["number"] == 3, "포함건수가 다릅니다.")

    # 개념태그·지식유형 같은 기록 표 속성이 섞여 들어가면 안 된다
    leaked = [name for name in ("개념명", "개념태그", "지식유형", "단원") if name in properties]
    check(not leaked, f"학습 기록 표의 속성이 섞였습니다: {leaked}")

    check("✅" in flow.text and "노션에 저장했습니다" in flow.text,
          f"저장 완료 안내가 아닙니다: {flow.text!r}")
    check(not SENT, "추출본을 저장하면서 학습 기록 표에도 보냈습니다.")


@case("/export 블록 100개 초과 → 이어붙이기로 나눠 보낸다 (§17-2)")
async def test_export_block_split():
    posted, patched = install_export_fakes([EXPORT_PAGE] * 60)

    flow = Flow()
    await walk_to_deliver(flow)
    await flow.press("📄 노션에 저장")

    _, payload = posted[0]
    check(len(payload["children"]) == bot.BLOCK_LIMIT,
          f"첫 요청의 블록이 {len(payload['children'])}개입니다 (최대 {bot.BLOCK_LIMIT}).")
    check(patched, "블록이 100개를 넘었는데 이어붙이기를 하지 않았습니다.")
    for path, body in patched:
        check(path == "/blocks/TEST_EXPORT_PAGE/children", f"이어붙일 곳이 다릅니다: {path}")
        check(len(body["children"]) <= bot.BLOCK_LIMIT,
              f"이어붙이기 한 번에 {len(body['children'])}개를 보냈습니다.")

    total = len(payload["children"]) + sum(len(body["children"]) for _, body in patched)
    check(total > bot.BLOCK_LIMIT, "나눠 보낸 블록 수가 100개 이하입니다.")


@case("/export [📥 파일로 받기] → §16-6 안내 문구로 바뀌었다")
async def test_export_file_notice():
    install_export_fakes([EXPORT_PAGE] * 2)

    flow = Flow()
    await walk_to_deliver(flow)
    await flow.press("📥 파일로 받기")

    check(len(flow.documents) == 1, f"파일이 1개여야 하는데 {len(flow.documents)}개입니다.")
    name, text = flow.documents[0]
    check(name.endswith(".md"), f"파일 이름이 .md가 아닙니다: {name}")
    check("공통매입세액" in text, "파일 내용에 기록이 없습니다.")

    # 명세서 §16-6 — 예전 문구는 오해를 낳아 교체되었다
    check("붙여넣으면 같은 구조로 저장됩니다" not in flow.text,
          f"교체 대상인 예전 문구가 그대로 남아 있습니다: {flow.text!r}")
    check("봇에 다시 보내도 저장되지 않습니다" in flow.text,
          f"§16-6 문구가 아닙니다: {flow.text!r}")
    check("노션에 모아 두려면" in flow.text, f"§16-6 문구가 아닙니다: {flow.text!r}")


# ============================================================
# 18. 네트워크 실패 → 한국어 안내 (스택트레이스 노출 없음)
# ============================================================
@case("네트워크 실패 → 세 파일이 함께 쓰는 한국어 안내가 나온다")
async def test_network_failure_korean():
    import requests

    import notion_common

    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.notion.com', port=443): Max retries exceeded "
            "(Caused by NameResolutionError('Failed to resolve api.notion.com'))"
        )

    saved_get, saved_request, saved_interval = (
        requests.get, requests.request, notion_common.REQUEST_INTERVAL
    )
    try:
        requests.get = boom
        requests.request = boom
        notion_common.REQUEST_INTERVAL = 0
        res = notion_common.notion_get("/databases/x", "token")
        message = notion_common.explain_error(res)
    finally:
        requests.get, requests.request = saved_get, saved_request
        notion_common.REQUEST_INTERVAL = saved_interval

    check(not res.ok, "연결 실패인데 성공으로 처리되었습니다.")
    check(len(calls) == 1, f"자동 재시도를 하면 안 되는데 {len(calls)}번 보냈습니다.")

    # 한국어 안내이고, 확인할 곳을 알려 준다
    for expected in ("인터넷 연결을 확인해 주세요", "Wi-Fi", "방화벽"):
        check(expected in message, f"안내에 '{expected}'가 없습니다:\n{message}")

    # 영어 원문·스택트레이스가 사용자 화면 문구에 섞이면 안 된다
    for leaked in ("Traceback", "ConnectionError", "HTTPSConnectionPool",
                   "NameResolutionError", "Max retries"):
        check(leaked not in message, f"영어 원문 '{leaked}'가 안내에 노출되었습니다:\n{message}")

    # 같은 안내를 세 파일이 함께 쓰는가 (bot.py·check_notion.py·setup_notion.py)
    import check_notion
    import setup_notion
    for module in (bot, check_notion, setup_notion):
        check(module.explain_error(res) == message,
              f"{module.__name__}이 다른 안내를 씁니다.")

    # fetch_data_source도 예외를 올려보내지 않고 한국어 안내로 바꾼다
    try:
        requests.get = boom
        notion_common.REQUEST_INTERVAL = 0
        ds_id, properties, error = notion_common.fetch_data_source("x", "token")
    finally:
        requests.get = saved_get
        notion_common.REQUEST_INTERVAL = saved_interval
    check(ds_id is None and error == message, f"fetch_data_source의 안내가 다릅니다: {error!r}")


# ============================================================
# 19. UTF-8 고정 — 한글 왕복 (명세서 §14-4)
# ============================================================
# 맥에서는 인코딩 문제가 재현되지 않는다. 그래서 이 검사는 "윈도우에서도 된다"를
# 증명하지 못한다. 증명하는 것은 **코드가 인코딩을 시스템 기본값에 맡기지
# 않는다**는 것뿐이다. 아래는 그 지점을 하나씩 짚는다.
@case("UTF-8 고정 — 한글 파일 쓰기→읽기 왕복과 인코딩 명시 (§14-4)")
async def test_utf8_roundtrip():
    import inspect

    folder = tempfile.mkdtemp(prefix="cpa_utf8_")
    한글 = "겸영사업자 세액계산 · 공통매입세액 안분 — ①②③ ✔️"

    # 1) 백업 파일(JSON) 왕복 — ensure_ascii=False라 \uXXXX로 깨져 들어가지 않는다
    saved_path = bot.PENDING_PATH
    bot.PENDING_PATH = os.path.join(folder, "pending_records.json")
    try:
        item = bot.build_item({
            "과목": "세법", "대분류": "부가가치세", "단원": "ch07.겸영사업자 세액계산",
            "개념태그": ["공통사용재화"], "지식유형": "법령",
            "정리 내용": 한글,
        }, None)
        bot.write_pending([item])

        raw = open(bot.PENDING_PATH, encoding="utf-8").read()
        check(한글 in raw, "백업 파일에 한글이 그대로 들어가지 않았습니다 (ensure_ascii 확인).")
        check("\\u" not in raw, f"한글이 \\uXXXX로 깨져 저장되었습니다:\n{raw[:200]}")

        back, broken = bot.read_pending()
        check(broken is None, "멀쩡한 백업 파일을 손상으로 판정했습니다.")
        check(back[0]["정리 내용"] == 한글,
              f"쓰기→읽기 왕복이 어긋났습니다: {back[0]['정리 내용']!r}")
    finally:
        bot.PENDING_PATH = saved_path

    # 2) .env 기록 왕복 — 경로·값에 한글이 섞여도 그대로 돌아온다
    env_path = os.path.join(folder, "한글폴더.env")
    bot.save_env_value(env_path, "NOTION_DB_ID", "8f2c1d4e5a6b7c8d9e0f1a2b3c4d5e6f")
    bot.save_env_value(env_path, "메모", 한글)
    text = open(env_path, encoding="utf-8").read()
    check(한글 in text, f".env에 한글이 그대로 쓰이지 않았습니다:\n{text!r}")
    bot.save_env_value(env_path, "NOTION_DB_ID", "새로운아이디")
    text = open(env_path, encoding="utf-8").read()
    check("NOTION_DB_ID=새로운아이디" in text, f"같은 키를 고쳐 쓰지 못했습니다:\n{text!r}")
    check(한글 in text, "한 줄을 고치면서 다른 줄의 한글이 깨졌습니다.")

    # 3) /export 파일 — 텔레그램으로 보내는 바이트가 UTF-8로 고정되어 있다
    install_export_fakes([EXPORT_PAGE])
    flow = Flow()
    await walk_to_deliver(flow)
    await flow.press("📥 파일로 받기")
    name, content = flow.documents[0]
    check("공통매입세액" in content, f"내보낸 파일에 한글이 없습니다: {content[:120]!r}")

    # 4) 인코딩을 시스템 기본값에 맡긴 곳이 남아 있지 않은가
    #    (맥에서는 기본값이 UTF-8이라 통과해 버리므로, 문자열로 직접 확인한다)
    for module in (bot, check_notion, setup_notion, notion_common):
        source = inspect.getsource(module)
        for line_no, line in enumerate(source.split("\n"), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in stripped:
                continue
            opens = "open(" in stripped and "codecs" not in stripped
            loads = "load_dotenv(" in stripped
            if (opens or loads) and "encoding=" not in stripped:
                raise AssertionError(
                    f"{module.__name__} {line_no}번째 줄에 인코딩이 명시되지 않았습니다:\n"
                    f"      {stripped}"
                )

    # 5) 터미널 출력도 UTF-8로 고정한다 (윈도우 cp949에서 이모지가 터진다)
    check(hasattr(notion_common, "force_utf8_console"),
          "터미널 출력 인코딩을 고정하는 함수가 없습니다.")
    notion_common.force_utf8_console()  # 두 번 불러도 터지지 않아야 한다


# ============================================================
# 20. /setup — 표 고르기 (명세서 §7)
# ============================================================
def data_source_result(title, properties, db_id):
    """노션 검색(POST /search)이 돌려주는 data_source 한 건의 모양."""
    return {
        "object": "data_source",
        "id": f"ds-{db_id}",
        "title": [{"plain_text": title}],
        "parent": {"type": "database_id", "database_id": db_id},
        "properties": properties,
    }


def record_schema():
    """§1 속성 10개를 갖춘 정상 학습 기록 표."""
    schema = {name: {"type": kind} for name, kind in notion_common.RECORD_PROPERTIES.items()}
    for name, options in OPTIONS.items():
        kind = notion_common.RECORD_PROPERTIES[name]
        schema[name] = {"type": kind, kind: {"options": [{"name": o} for o in options]}}
    return schema


def export_schema():
    """§17 추출본 표. /setup 목록에 함께 나오므로 반드시 걸러져야 한다."""
    return {name: {"type": kind} for name, kind in notion_common.EXPORT_PROPERTIES.items()}


def install_setup_fakes(results, error=None):
    """POST /search를 바꿔친다. (부른 기록, 되돌리는 함수)."""
    calls = []
    saved_post = bot.notion_post

    def fake_notion_post(path, token, payload):
        calls.append((path, payload))
        if error is not None:
            return FakeErrorResponse(error)
        return FakeResponse({"results": list(results), "has_more": False})

    bot.notion_post = fake_notion_post
    return calls, lambda: setattr(bot, "notion_post", saved_post)


class FakeErrorResponse:
    ok = False

    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "fake error"

    def json(self):
        return {}


def use_temp_env():
    """.env 기록을 임시 파일로 돌린다. 진짜 .env에는 절대 손대지 않는다."""
    saved = bot.ENV_PATH
    bot.ENV_PATH = os.path.join(tempfile.mkdtemp(prefix="cpa_env_"), ".env")
    return bot.ENV_PATH, lambda: setattr(bot, "ENV_PATH", saved)


def clear_db_id():
    """.env의 NOTION_DB_ID가 비어 있는 상태(= 배포 직후)를 만든다."""
    saved = bot.NOTION_DB_ID
    bot.NOTION_DB_ID = ""
    return lambda: setattr(bot, "NOTION_DB_ID", saved)


@case("/setup — 표 목록을 버튼으로 보여 주고, 고르면 .env에 기록한다 (§7)")
async def test_setup_pick():
    calls, undo_post = install_setup_fakes([
        data_source_result("CPA 추출본", export_schema(), "db-export"),
        data_source_result("CPA 지식 OS", record_schema(), "db-record"),
    ])
    env_path, undo_env = use_temp_env()
    undo_db = clear_db_id()
    saved_ds, saved_options = bot.DATA_SOURCE_ID, bot.OPTIONS
    try:
        flow = Flow()
        await flow.command(bot.cmd_setup)

        check(calls and calls[0][0] == "/search", f"검색을 부르지 않았습니다: {calls}")
        # 노션에 쓰기를 하면 안 된다 — /search 말고 다른 POST가 있으면 실패다
        others = [path for path, _ in calls if path != "/search"]
        check(not others, f"/setup이 노션에 쓰기를 했습니다: {others}")

        check(flow.has("CPA 지식 OS"), f"표 버튼이 없습니다: {flow.labels()}")
        check(flow.has("CPA 추출본"), f"표 버튼이 없습니다: {flow.labels()}")

        await flow.press("CPA 지식 OS")

        check("✅" in flow.text, f"성공 안내가 아닙니다: {flow.text!r}")
        check(bot.NOTION_DB_ID == "db-record", f".env용 db_id가 다릅니다: {bot.NOTION_DB_ID!r}")
        check(bot.DATA_SOURCE_ID == "ds-db-record",
              f"data source id가 다릅니다: {bot.DATA_SOURCE_ID!r}")
        check(bot.OPTIONS["지식유형"] == OPTIONS["지식유형"],
              f"선택지를 읽지 못했습니다: {bot.OPTIONS}")

        written = open(env_path, encoding="utf-8").read()
        check("NOTION_DB_ID=db-record" in written, f".env에 기록되지 않았습니다:\n{written!r}")
    finally:
        undo_post(); undo_env(); undo_db()
        bot.DATA_SOURCE_ID, bot.OPTIONS = saved_ds, saved_options


@case("/setup — 추출본 표를 고르면 속성 검사로 잡아 한국어로 안내한다 (§17)")
async def test_setup_wrong_table():
    calls, undo_post = install_setup_fakes([
        data_source_result("CPA 추출본", export_schema(), "db-export"),
    ])
    env_path, undo_env = use_temp_env()
    undo_db = clear_db_id()
    try:
        flow = Flow()
        await flow.command(bot.cmd_setup)
        await flow.press("CPA 추출본")

        check("추출본" in flow.text, f"추출본 표라고 짚어 주지 않았습니다: {flow.text!r}")
        check("/setup" in flow.text, f"다시 고르는 방법을 알려 주지 않았습니다: {flow.text!r}")
        check(bot.NOTION_DB_ID == "", f"잘못 고른 표가 적용되었습니다: {bot.NOTION_DB_ID!r}")
        check(not os.path.exists(env_path), "잘못 고른 표의 ID가 .env에 기록되었습니다.")

        # 영어 원문이 새어 나오면 안 된다
        for leaked in ("Traceback", "KeyError", "properties"):
            check(leaked not in flow.text, f"영어 원문 '{leaked}'가 노출되었습니다: {flow.text!r}")
    finally:
        undo_post(); undo_env(); undo_db()


@case("/setup — 속성이 모자란 표를 고르면 무엇이 없는지 알려 준다")
async def test_setup_missing_property():
    broken = record_schema()
    del broken["개념태그"]
    broken["기록일"] = {"type": "date"}
    calls, undo_post = install_setup_fakes([
        data_source_result("엉뚱한 표", broken, "db-bad"),
    ])
    env_path, undo_env = use_temp_env()
    undo_db = clear_db_id()
    try:
        flow = Flow()
        await flow.command(bot.cmd_setup)
        await flow.press("엉뚱한 표")

        check("개념태그" in flow.text, f"없는 속성을 짚어 주지 않았습니다: {flow.text!r}")
        check("기록일" in flow.text, f"타입이 다른 속성을 짚어 주지 않았습니다: {flow.text!r}")
        check("created_time" in flow.text, f"기대 타입을 알려 주지 않았습니다: {flow.text!r}")
        check(bot.NOTION_DB_ID == "", "규격이 다른 표가 적용되었습니다.")
        check(not os.path.exists(env_path), "규격이 다른 표의 ID가 .env에 기록되었습니다.")
    finally:
        undo_post(); undo_env(); undo_db()


@case("/setup — 검색이 실패하거나 결과가 비면 수동 입력 방법을 한국어로 안내한다")
async def test_setup_search_failure():
    undo_db = clear_db_id()
    try:
        # (1) 검색 자체가 실패 (401)
        calls, undo_post = install_setup_fakes([], error=401)
        flow = Flow()
        await flow.command(bot.cmd_setup)
        undo_post()
        joined = "\n".join(flow.screen.replies)
        check("NOTION_DB_ID=" in joined, f"수동 입력 방법이 없습니다:\n{joined}")
        check("링크 복사" in joined, f"주소를 어디서 복사하는지 없습니다:\n{joined}")
        check("32자리" in joined, f"주소의 어느 부분인지 알려 주지 않았습니다:\n{joined}")

        # (2) 결과가 비었음 — 통합 연결을 안 한 가장 흔한 경우
        calls, undo_post = install_setup_fakes([])
        flow = Flow()
        await flow.command(bot.cmd_setup)
        undo_post()
        joined = "\n".join(flow.screen.replies)
        check("연결(Connections)" in joined, f"통합 연결 안내가 없습니다:\n{joined}")
        check("NOTION_DB_ID=" in joined, f"수동 입력 방법이 없습니다:\n{joined}")
    finally:
        undo_db()


@case("/setup — 표가 10개를 넘으면 단원 화면과 같은 쪽 넘김이 걸린다 (§7)")
async def test_setup_pagination():
    results = [
        data_source_result(f"표{i:02}", record_schema(), f"db-{i:02}")
        for i in range(1, 15)
    ]
    calls, undo_post = install_setup_fakes(results)
    env_path, undo_env = use_temp_env()
    undo_db = clear_db_id()
    saved_ds, saved_options = bot.DATA_SOURCE_ID, bot.OPTIONS
    try:
        flow = Flow()
        await flow.command(bot.cmd_setup)

        labels = flow.labels()
        check(sum(1 for one in labels if one.startswith("표")) == bot.PAGE_SIZE,
              f"한 쪽에 {bot.PAGE_SIZE}개여야 하는데 다릅니다: {labels}")
        check(flow.has("1/2"), f"쪽 표시가 없습니다: {labels}")
        check(flow.has("다음 ▶"), f"쪽 넘김 버튼이 없습니다: {labels}")

        await flow.press("다음 ▶")
        check(flow.has("2/2"), f"쪽이 넘어가지 않았습니다: {flow.labels()}")
        check(flow.has("표14"), f"둘째 쪽에 나머지가 없습니다: {flow.labels()}")

        # 둘째 쪽에서 고른 것이 그 표여야 한다 (원래 인덱스가 유지되는가)
        await flow.press("표14")
        check(bot.NOTION_DB_ID == "db-14", f"엉뚱한 표가 골라졌습니다: {bot.NOTION_DB_ID!r}")
    finally:
        undo_post(); undo_env(); undo_db()
        bot.DATA_SOURCE_ID, bot.OPTIONS = saved_ds, saved_options


@case("/setup — /new·/export 진행 중에는 안내만 하고 동작하지 않는다")
async def test_setup_blocked_during_flows():
    calls, undo_post = install_setup_fakes([
        data_source_result("CPA 지식 OS", record_schema(), "db-record"),
    ])
    try:
        # /new 진행 중
        flow = Flow()
        await walk_to_tag_step(flow)
        await flow.command(bot.cmd_setup)
        check(not calls, f"/new 중인데 검색을 불렀습니다: {calls}")
        check(flow.screen.replies and "/new" in flow.screen.replies[-1],
              f"/new 진행 중 안내가 아닙니다: {flow.screen.replies}")
        check(flow.session is not None, "/setup이 진행 중인 입력을 망가뜨렸습니다.")

        # /export 진행 중
        install_export_fakes([EXPORT_PAGE])
        flow = Flow()
        await flow.command(bot.cmd_export)
        calls.clear()
        await flow.command(bot.cmd_setup)
        check(not calls, f"/export 중인데 검색을 불렀습니다: {calls}")
        check(flow.screen.replies and "/export" in flow.screen.replies[-1],
              f"/export 진행 중 안내가 아닙니다: {flow.screen.replies}")
        check(flow.screen.user_data.get("export") is not None,
              "/setup이 진행 중인 내보내기를 망가뜨렸습니다.")
    finally:
        undo_post()


@case("DB ID가 비어 있으면 /new·/export가 /setup 안내로 막힌다 (있으면 그대로 동작 — 회귀)")
async def test_not_ready_guard():
    undo_db = clear_db_id()
    try:
        flow = Flow()
        await flow.command(bot.cmd_new)
        check(flow.session is None, "표가 없는데 입력 흐름이 시작되었습니다.")
        check(flow.screen.replies and "/setup" in flow.screen.replies[-1],
              f"/setup 안내가 아닙니다: {flow.screen.replies}")

        flow = Flow()
        await flow.command(bot.cmd_export)
        check(flow.screen.user_data.get("export") is None,
              "표가 없는데 내보내기가 시작되었습니다.")
        check(flow.screen.replies and "/setup" in flow.screen.replies[-1],
              f"/setup 안내가 아닙니다: {flow.screen.replies}")
    finally:
        undo_db()

    # 회귀 — DB ID가 있으면 전과 똑같이 동작한다
    flow = Flow()
    await flow.start()
    check(flow.session is not None, "DB ID가 있는데 입력 흐름이 시작되지 않았습니다.")
    check("과목" in flow.text, f"첫 화면이 아닙니다: {flow.text!r}")


# ============================================================
# 21. /export 수식 폴백 (명세서 §13-3)
# ============================================================
@case("/export 노션 저장 — 수식이 400을 내면 코드블록으로 대체하고 알린다 (§13-3)")
async def test_export_equation_fallback():
    page = copy.deepcopy(EXPORT_PAGE)
    saved_blocks = bot.fetch_blocks
    posted, patched = install_export_fakes([page])

    # 본문에 수식 두 개가 들어 있는 기록
    def fake_fetch_blocks(block_id):
        return [
            {"type": "equation", "equation": {"expression": "x = 1"}},
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "설명 문단"}]}},
            {"type": "equation", "equation": {"expression": "y = 2"}},
        ], None

    bot.fetch_blocks = fake_fetch_blocks

    # 첫 요청은 400(수식 거부), 두 번째는 성공 — send_record와 같은 2단계 폴백
    attempts = []

    def fake_notion_post(path, token, payload):
        attempts.append(payload)
        if len(attempts) == 1:
            return FakeErrorResponse(400)
        return FakeResponse({"id": "TEST_EXPORT_PAGE", "url": "https://notion.example/TEST"})

    bot.notion_post = fake_notion_post
    try:
        flow = Flow()
        await walk_to_deliver(flow)
        await flow.press("📄 노션에 저장")

        check(len(attempts) == 2, f"폴백 재시도를 하지 않았습니다 (요청 {len(attempts)}회).")

        # 1차에는 수식이 남아 있고, 2차에는 모두 code로 바뀌어 있다
        first = [b["type"] for b in attempts[0]["children"]]
        second = [b["type"] for b in attempts[1]["children"]]
        check("equation" in first, f"1차 요청에 수식이 없습니다: {first}")
        check("equation" not in second, f"2차 요청에 수식이 남아 있습니다: {second}")
        check(second.count("code") == 2, f"수식 2개가 코드블록이 되지 않았습니다: {second}")

        # 저장은 성공하고, 대체 사실을 사용자에게 알린다
        check("노션에 저장했습니다" in flow.text, f"저장에 실패했습니다: {flow.text!r}")
        check("코드블록으로 저장했습니다" in flow.text,
              f"대체 사실을 알리지 않았습니다: {flow.text!r}")
        check("수식 1·2번" in flow.text, f"번호 안내가 다릅니다: {flow.text!r}")
    finally:
        bot.fetch_blocks = saved_blocks


@case("/export 수식 폴백 — 빈 수식은 보내기 전에 코드블록으로 바뀐다 (1단계)")
async def test_export_empty_equation():
    saved_blocks = bot.fetch_blocks
    posted, patched = install_export_fakes([copy.deepcopy(EXPORT_PAGE)])

    def fake_fetch_blocks(block_id):
        return [{"type": "equation", "equation": {"expression": "   "}}], None

    bot.fetch_blocks = fake_fetch_blocks
    try:
        flow = Flow()
        await walk_to_deliver(flow)
        await flow.press("📄 노션에 저장")

        check(len(posted) == 1, f"한 번에 보내야 하는데 {len(posted)}번 보냈습니다.")
        kinds = [b["type"] for b in posted[0][1]["children"]]
        check("equation" not in kinds, f"빈 수식을 그대로 보냈습니다: {kinds}")
        check("code" in kinds, f"빈 수식이 코드블록이 되지 않았습니다: {kinds}")
        check("코드블록으로 저장했습니다" in flow.text,
              f"대체 사실을 알리지 않았습니다: {flow.text!r}")
    finally:
        bot.fetch_blocks = saved_blocks


# ============================================================
# 22. /raw 배포 처리
# ============================================================
@case("/raw — 진단 도구임을 밝히고, 결과 끝에 /new 안내가 붙는다")
async def test_raw_notice():
    flow = Flow()
    await flow.command(bot.cmd_raw)

    entry = "\n".join(flow.screen.replies)
    check("진단" in entry, f"진단용임을 밝히지 않았습니다: {entry!r}")
    check("저장되지 않습니다" in entry or "저장하는 명령이 아닙니다" in entry,
          f"기록되지 않는다는 점을 밝히지 않았습니다: {entry!r}")

    await flow.say("$$ x = 1 $$\n- 항목")

    check("이 화면은 문제 진단용입니다" in flow.text,
          f"결과 끝 안내가 없습니다: {flow.text!r}")
    check("/new" in flow.text, f"결과 끝 안내에 /new가 없습니다: {flow.text!r}")

    # 기존 동작은 그대로다 — 원문·entities·특수문자 요약을 여전히 보여 준다
    check(flow.screen.msg_id >= 4, "진단 결과 화면 수가 줄었습니다.")


# ============================================================
# 실행
# ============================================================
async def run_all():
    print("[안내] test_flow.py — 텔레그램·노션 없이 봇의 입력 흐름을 검사합니다.")
    print(f"   검사 항목 {len(CASES)}개\n")

    failed = []
    for number, (name, func) in enumerate(CASES, start=1):
        SENT.clear()
        bot.write_pending([])
        try:
            await func()
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  {number:2}) {name}\n      [오류] 실패 — {e}")
        except Exception:
            failed.append((name, traceback.format_exc()))
            print(f"  {number:2}) {name}\n      [오류] 오류 —\n{traceback.format_exc()}")
        else:
            print(f"  {number:2}) {name} — [완료] 통과")

    print("\n--- 요약 ---")
    if failed:
        print(f"[오류] {len(CASES)}개 중 {len(failed)}개가 실패했습니다.")
        for name, _ in failed:
            print(f"   · {name}")
        return 1

    print(f"[완료] {len(CASES)}개 항목이 모두 통과했습니다.")
    return 0


def main():
    install_fakes()
    return asyncio.run(run_all())


if __name__ == "__main__":
    raise SystemExit(main())
