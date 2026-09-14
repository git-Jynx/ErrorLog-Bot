"""노션에 CPA 지식 OS 데이터베이스(표)를 만드는 스크립트.

docs/notion_template_spec.md 의 "1. 데이터베이스 속성"과
"2. 선택형 속성의 옵션값"을 그대로 반영합니다.
"""

import os
import re
import sys

import yaml
from dotenv import load_dotenv

from notion_common import (
    EXPORT_DB_TITLE,
    EXPORT_ENV_KEY,
    NETWORK_FAILED,
    create_export_database,
    explain_error,
    force_utf8_console,
    notion_get,
    notion_post,
    save_env_value,
)

# 윈도우 명령 프롬프트(cp949)에서 이모지를 찍다 죽지 않게 한다 (명세서 §14-4).
force_utf8_console()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
CHAPTERS_PATH = os.path.join(BASE_DIR, "chapters.yaml")

DB_TITLE = "CPA 지식 OS"

KNOWLEDGE_TYPES = ["기준서", "법령", "이론·개념", "산식", "암기", "견해"]
ERROR_TYPES = ["개념 미숙지", "개념 혼동", "조건 오독", "계산 실수", "함정 미인지", "시간부족", "근거오류"]
SOURCE_TYPES = ["기출", "객관식", "연습서", "모의고사", "AI질의"]


def load_subjects():
    """chapters.yaml 최상위 키(과목 이름)를 순서대로 읽는다."""
    with open(CHAPTERS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return list(data.keys())


def build_properties(subjects):
    """명세서 1번 표의 속성 10개를 명세 순서대로 만든다."""

    def select(names):
        return {"select": {"options": [{"name": n} for n in names]}}

    def multi_select(names):
        return {"multi_select": {"options": [{"name": n} for n in names]}}

    return {
        "개념명": {"title": {}},
        "과목": select(subjects),
        "단원": select([]),          # 저장 시 자동 생성
        "개념태그": multi_select([]),  # 저장 시 자동 생성 (v1.3 다중 선택)
        "지식유형": select(KNOWLEDGE_TYPES),
        "오답유형": multi_select(ERROR_TYPES),  # v1.3 다중 선택
        "교재구분": select(SOURCE_TYPES),
        "문제위치": {"rich_text": {}},
        "참고출처": {"rich_text": {}},
        "기록일": {"created_time": {}},
    }


def page_title(page):
    for value in page.get("properties", {}).values():
        if value.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in value["title"]) or "(제목 없음)"
    return "(제목 없음)"


def search_pages(token):
    """통합이 접근할 수 있는 페이지 목록을 가져온다. 표의 행(row)은 제외한다.

    (페이지 목록, 인터넷이 끊겼는가)를 돌려준다. 실패했으면 목록은 None이다.
    인터넷이 끊긴 것과 그 밖의 실패는 다르게 다뤄야 한다 — 연결이 안 되는데
    페이지 주소를 물어 봐야 그 다음 단계도 똑같이 실패한다.
    """
    pages = []
    cursor = None
    while True:
        payload = {"filter": {"property": "object", "value": "page"}, "page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = notion_post("/search", token, payload)
        if not res.ok:
            print(explain_error(res))
            return None, res.status_code == NETWORK_FAILED
        data = res.json()
        for page in data.get("results", []):
            if page.get("parent", {}).get("type") in ("page_id", "workspace"):
                pages.append(page)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages, False


def extract_id_from_url(url):
    """노션 페이지 주소에서 32자리 ID를 뽑아낸다."""
    found = re.findall(r"[0-9a-fA-F]{32}", url.replace("-", ""))
    return found[-1] if found else None


def ask_page_url():
    print("\n페이지 주소를 직접 붙여넣어 주세요.")
    print("  (노션에서 페이지를 열고 우측 상단 ... → '링크 복사')")
    url = input("페이지 주소: ").strip()
    page_id = extract_id_from_url(url)
    if not page_id:
        print("[오류] 주소에서 페이지 ID를 찾지 못했습니다. 주소 전체를 복사했는지 확인해 주세요.")
        return None
    return page_id


def choose_parent_page(token):
    """표를 만들 상위 페이지를 고른다. 검색 → 실패하면 주소 직접 입력."""
    print("[안내] 통합이 접근할 수 있는 노션 페이지를 찾는 중입니다...")
    pages, network_down = search_pages(token)

    if network_down:
        # 연결이 안 되는 상태에서 주소를 물어 봐야 다음 단계도 똑같이 실패한다.
        print("\n인터넷 연결을 확인한 뒤 setup_notion.py를 다시 실행해 주세요.")
        return None

    if not pages:
        if pages is None:
            print("검색에 실패했습니다.")
        else:
            print("접근 가능한 페이지가 없습니다.")
            print("노션에서 페이지를 열고 ... → 연결(Connections) 에 통합을 추가했는지 확인해 주세요.")
        return ask_page_url()

    print("\n표를 만들 페이지를 번호로 골라 주세요.")
    for i, page in enumerate(pages, 1):
        print(f"  {i}. {page_title(page)}")
    print("  0. 목록에 없음 (페이지 주소를 직접 입력)")

    answer = input("\n번호 입력: ").strip()
    if not answer.isdigit() or int(answer) > len(pages):
        print("[오류] 목록에 있는 번호를 입력해 주세요.")
        return None
    if answer == "0":
        return ask_page_url()
    return pages[int(answer) - 1]["id"]


def save_db_id(db_id):
    """.env 의 NOTION_DB_ID 줄에 값을 기록한다."""
    with open(ENV_PATH, encoding="utf-8") as f:
        lines = f.read().splitlines()

    for i, line in enumerate(lines):
        if line.startswith("NOTION_DB_ID"):
            lines[i] = f"NOTION_DB_ID={db_id}"
            break
    else:
        lines.append(f"NOTION_DB_ID={db_id}")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    load_dotenv(ENV_PATH, encoding="utf-8")

    if os.getenv("NOTION_DB_ID", "").strip():
        print("이미 표가 등록되어 있습니다.")
        print(f"  .env의 NOTION_DB_ID = {os.getenv('NOTION_DB_ID').strip()}")
        print("  표를 새로 만들려면 .env에서 NOTION_DB_ID 값을 지우고 다시 실행하세요.")
        return

    token = os.getenv("NOTION_TOKEN", "").strip()
    if not token:
        print("[오류] .env 파일에 NOTION_TOKEN이 없습니다.")
        print("   1) .env.example 을 복사해 .env 파일을 만드세요.")
        print("   2) https://www.notion.so/my-integrations 에서 통합의 토큰을 복사해")
        print("      NOTION_TOKEN= 뒤에 붙여넣으세요.")
        return

    subjects = load_subjects()
    print(f"[안내] chapters.yaml에서 과목 {len(subjects)}개를 읽었습니다: {', '.join(subjects)}")

    parent_page_id = choose_parent_page(token)
    if not parent_page_id:
        return

    properties = build_properties(subjects)
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": DB_TITLE}}],
        "initial_data_source": {"properties": properties},
    }

    print("\n[안내] 표를 만드는 중입니다...")
    res = notion_post("/databases", token, payload)
    if not res.ok:
        print(explain_error(res))
        sys.exit(1)

    created = res.json()
    db_id = created["id"]
    save_db_id(db_id)

    print(f"\n[완료] 표를 만들었습니다. (제목: {DB_TITLE})")
    print(f"   database id: {db_id}")
    print("   이 값을 .env의 NOTION_DB_ID에 기록했습니다.")

    # 노션이 실제로 만든 속성을 다시 읽어와 명세서와 대조한다.
    res = notion_get(f"/data_sources/{created['data_sources'][0]['id']}", token)
    if not res.ok:
        print(explain_error(res))
        sys.exit(1)
    schema = res.json()["properties"]

    print("\n[안내] 노션에 실제로 만들어진 속성")
    for i, name in enumerate(properties, 1):
        actual = schema.get(name)
        if actual is None:
            print(f"  {i:2}. {name} — [오류] 만들어지지 않았습니다")
            continue
        options = actual.get(actual["type"], {}).get("options")
        tail = f" · 선택지 {len(options)}개" if options else ""
        print(f"  {i:2}. {name} ({actual['type']}){tail}")

    create_export_table(parent_page_id, token)


def create_export_table(parent_page_id, token):
    """추출본 표를 같은 페이지 아래에 함께 만든다 (명세서 §17-3).

    /export의 [📄 노션에 저장]이 쓰는 표다. 여기서 실패해도 학습 기록 표는
    이미 만들어졌으므로 프로그램을 끝내지 않는다. 봇이 처음 저장할 때
    다시 만들어 본다.
    """
    print(f"\n[안내] 추출본 표('{EXPORT_DB_TITLE}')를 만드는 중입니다...")
    export_db_id, error = create_export_database(parent_page_id, token)
    if error:
        print(error)
        print("   [안내] 추출본 표는 만들지 못했지만 학습 기록 표는 정상입니다.")
        print("   /export에서 [노션에 저장]을 처음 누를 때 봇이 다시 만듭니다.")
        return

    save_env_value(ENV_PATH, EXPORT_ENV_KEY, export_db_id)
    print(f"[완료] 추출본 표를 만들었습니다. (제목: {EXPORT_DB_TITLE})")
    print(f"   database id: {export_db_id}")
    print(f"   이 값을 .env의 {EXPORT_ENV_KEY}에 기록했습니다.")


if __name__ == "__main__":
    main()
