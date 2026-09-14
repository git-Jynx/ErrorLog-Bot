"""노션 표가 docs/notion_template_spec.md 명세와 일치하는지 검사하는 스크립트.

읽기 전용입니다. 노션 표를 수정하지 않습니다.
"""

import os
import sys

import yaml
from dotenv import load_dotenv

from notion_common import (
    EXPORT_DB_TITLE,
    EXPORT_ENV_KEY,
    EXPORT_PROPERTIES,
    NOTION_VERSION,
    explain_error,
    fetch_data_source,
    force_utf8_console,
    notion_get,
)

# 윈도우 명령 프롬프트(cp949)에서 이모지를 찍다 죽지 않게 한다 (명세서 §14-4).
force_utf8_console()

# ============================================================
# 명세서(docs/notion_template_spec.md) 기대값 - 명세가 바뀌면 이 블록만 고치면 됨
# ============================================================

EXPECTED_PROPERTIES = {
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

# v1.2부터 옵션의 원천은 노션이다(§9). 아래 목록은 setup_notion.py가 심는
# 초기값(시드)일 뿐이며, check_options()는 이와 다르다고 오류를 내지 않는다.
EXPECTED_OPTIONS = {
    "지식유형": ["기준서", "법령", "이론·개념", "산식", "암기", "견해"],
    "오답유형": ["개념 미숙지", "개념 혼동", "조건 오독", "계산 실수", "함정 미인지", "시간부족", "근거오류"],
    "교재구분": ["기출", "객관식", "연습서", "모의고사", "AI질의"],
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
CHAPTERS_PATH = os.path.join(BASE_DIR, "chapters.yaml")


def load_subjects():
    with open(CHAPTERS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return list(data.keys())


def get_data_source_schema(db_id, token):
    """database_id로 database를 조회한 뒤, 그 안의 첫 data source의 속성을 가져온다."""
    res = notion_get(f"/databases/{db_id}", token)
    if not res.ok:
        return None, res

    db = res.json()
    data_sources = db.get("data_sources", [])
    if not data_sources:
        return None, None

    res2 = notion_get(f"/data_sources/{data_sources[0]['id']}", token)
    if not res2.ok:
        return None, res2

    return res2.json().get("properties", {}), None


def check_properties(schema, results):
    """항목 2, 3: 속성 존재 여부와 타입 일치 여부."""
    for name, expected_type in EXPECTED_PROPERTIES.items():
        actual = schema.get(name)
        if actual is None:
            results.append((
                False,
                f"속성 누락: `{name}`",
                f"   명세서에는 있으나 노션 표에 없습니다.\n"
                f"   → 노션에서 `{name}` 속성을 타입({expected_type})에 맞게 추가해주세요.",
            ))
            continue

        actual_type = actual.get("type")
        if actual_type != expected_type:
            note = ""
            if name == "기록일" and actual_type == "date":
                note = "   → `기록일`은 반드시 '생성 일시(Created time)' 타입이어야 합니다. '날짜' 타입이면 저장 시 값이 비어 있게 됩니다.\n"
            results.append((
                False,
                f"속성 타입 불일치: `{name}`",
                f"   명세서: {expected_type} / 노션: {actual_type}\n"
                + note
                + f"   → 노션에서 `{name}` 속성의 타입을 {expected_type}(으)로 바꿔주세요.",
            ))
            continue

        results.append((True, f"속성 `{name}` ({expected_type}) 확인됨", None))


def check_options(schema, results):
    """항목 4 (v1.2/§8): 지식유형/오답유형/교재구분에 옵션이 있는지만 검사한다.
    옵션이 하나도 없으면 오류(봇 버튼을 만들 수 없음). 명세서 초기값과
    다른 것은 오류가 아니라 참고 표시만 한다 - 노션이 옵션의 원천이기 때문이다."""
    for name, expected_options in EXPECTED_OPTIONS.items():
        actual = schema.get(name)
        if actual is None:
            continue  # 이미 속성 누락으로 위에서 보고됨

        options = actual.get(actual.get("type"), {}).get("options", [])
        actual_names = [o["name"] for o in options]

        if not actual_names:
            results.append((
                False,
                f"옵션 없음: `{name}`",
                f"   이 속성에 옵션이 하나도 없어 봇이 버튼을 만들 수 없습니다.\n"
                f"   → 노션에서 `{name}` 속성을 열어 옵션을 최소 1개 이상 추가해주세요.\n"
                f"   (참고용 초기값: {', '.join(expected_options)})",
            ))
            continue

        missing = [o for o in expected_options if o not in actual_names]
        extra = [o for o in actual_names if o not in expected_options]

        if missing or extra:
            detail_lines = [f"   현재 노션 옵션: {', '.join(actual_names)}"]
            if missing:
                detail_lines.append(f"   명세서 초기값에는 있으나 노션에는 없음: {', '.join(missing)}")
            if extra:
                detail_lines.append(f"   명세서 초기값에는 없으나 노션에는 있음: {', '.join(extra)}")
            detail_lines.append("   → 오류가 아닙니다. v1.2부터 옵션의 기준은 노션이며, 명세서 값은 참고용 초기값일 뿐입니다.")
            results.append((
                True,
                f"참고: `{name}` 옵션이 명세서 초기값과 다름",
                "\n".join(detail_lines),
            ))
        else:
            results.append((True, f"`{name}` 옵션 {len(actual_names)}개 확인됨", None))


def check_subjects(schema, subjects, results):
    """항목 5: 과목 선택지가 chapters.yaml과 일치하는지 (없어도 오류 아님)."""
    actual = schema.get("과목")
    if actual is None:
        return  # 이미 속성 누락으로 보고됨

    options = actual.get(actual.get("type"), {}).get("options", [])
    actual_names = {o["name"] for o in options}
    missing = [s for s in subjects if s not in actual_names]

    if missing:
        results.append((
            True,
            f"과목 선택지 안내: {', '.join(missing)}",
            f"   chapters.yaml에는 있으나 노션에는 아직 없습니다.\n"
            f"   → 오류가 아닙니다. 해당 과목으로 처음 저장할 때 노션이 자동으로 옵션을 만듭니다.",
        ))
    else:
        results.append((True, "`과목` 선택지가 chapters.yaml과 일치함", None))


def check_extra_properties(schema, results):
    """항목 6: 명세서에 없는 속성이 추가로 있는지 (경고 수준)."""
    extra = [name for name in schema if name not in EXPECTED_PROPERTIES]
    if extra:
        results.append((
            True,
            f"경고: 명세서에 없는 속성 발견 - {', '.join(extra)}",
            "   오류는 아니지만, 명세서에 정의되지 않은 속성입니다. 필요 없다면 삭제를 고려해주세요.",
        ))


def check_export_db(results):
    """항목 7 (§17-3): 추출본 표는 **선택 사항**이다.

    `/export`를 한 번도 쓰지 않은 사용자에게는 표가 없는 것이 정상이므로,
    없다고 오류를 내지 않는다. .env에 ID가 적혀 있을 때만, 그 표가 실제로
    열리고 속성이 맞는지 확인한다.
    """
    export_db_id = os.getenv(EXPORT_ENV_KEY, "").strip()
    if not export_db_id:
        results.append((
            True,
            f"추출본 표 없음 — 정상입니다 (`{EXPORT_ENV_KEY}` 미설정)",
            f"   /export에서 [노션에 저장]을 처음 누를 때 봇이 `{EXPORT_DB_TITLE}` 표를\n"
            "   자동으로 만들고 .env에 ID를 적어 둡니다. 지금 하실 일은 없습니다.",
        ))
        return

    token = os.getenv("NOTION_TOKEN", "").strip()
    _, schema, error = fetch_data_source(export_db_id, token)
    if error:
        results.append((
            True,
            f"참고: 추출본 표를 열지 못했습니다 ({EXPORT_ENV_KEY})",
            f"{error}\n"
            f"   → 오류가 아닙니다. .env에서 {EXPORT_ENV_KEY} 값을 지우면\n"
            "      봇이 다음 내보내기 때 표를 새로 만듭니다.",
        ))
        return

    missing = [
        f"`{name}`({kind})" for name, kind in EXPORT_PROPERTIES.items()
        if (schema.get(name) or {}).get("type") != kind
    ]
    if missing:
        results.append((
            True,
            "참고: 추출본 표의 속성이 명세서(§17-2)와 다릅니다",
            f"   어긋난 속성: {', '.join(missing)}\n"
            "   → 오류가 아닙니다. 다만 이대로면 [노션에 저장]이 실패할 수 있습니다.",
        ))
    else:
        results.append((True, f"추출본 표 `{EXPORT_DB_TITLE}` 속성 {len(EXPORT_PROPERTIES)}개 확인됨", None))


def print_result(ok, title, detail):
    if ok:
        print(f"[완료] {title}")
    else:
        print(f"[오류] {title}")
        if detail:
            print(detail)


def main():
    load_dotenv(ENV_PATH, encoding="utf-8")
    token = os.getenv("NOTION_TOKEN", "").strip()
    db_id = os.getenv("NOTION_DB_ID", "").strip()

    if not token:
        print("[오류] .env 파일에 NOTION_TOKEN이 없습니다.")
        print("   https://www.notion.so/my-integrations 에서 토큰을 복사해 넣어주세요.")
        sys.exit(1)

    if not db_id:
        print("[오류] .env 파일에 NOTION_DB_ID가 없습니다.")
        print("   먼저 setup_notion.py를 실행하세요.")
        sys.exit(1)

    print("[안내] 노션 표에 접근하는 중입니다...")
    schema, err_res = get_data_source_schema(db_id, token)

    if err_res is not None:
        print(explain_error(err_res))
        sys.exit(1)

    if schema is None:
        print("[오류] 이 데이터베이스에 data source가 없습니다. 표가 올바르게 생성되었는지 확인해주세요.")
        sys.exit(1)

    print("[완료] 표에 정상적으로 접근했습니다.\n")

    results = []
    check_properties(schema, results)
    check_options(schema, results)

    subjects = load_subjects()
    check_subjects(schema, subjects, results)
    check_extra_properties(schema, results)
    check_export_db(results)

    print("--- 검사 결과 ---")
    for ok, title, detail in results:
        print_result(ok, title, detail)

    failed = [r for r in results if not r[0]]

    print("\n--- 요약 ---")
    if failed:
        print(f"[오류] 총 {len(failed)}건의 문제가 발견되었습니다. 위 안내를 따라 노션 표를 수정한 뒤 다시 실행해주세요.")
        sys.exit(1)
    else:
        print("[완료] 모든 항목이 명세서와 일치합니다.")
        sys.exit(0)


if __name__ == "__main__":
    main()
