"""백업 파일에 테스트용 미저장 기록 1건을 넣어 두는 스크립트.

봇을 껐다 켤 때 뜨는 재시작 알림과 [🔄 지금 저장] · [🗑 버리기] 버튼이
제대로 동작하는지 확인하는 용도입니다. 저장 실패를 일부러 만들어 내기가
어려우므로, 저장에 실패한 것과 똑같은 상태를 직접 만들어 줍니다.

.env와 노션에는 손대지 않습니다. pending_records.json 파일만 씁니다.
"""

import os
import sys

# 이 파일은 dev/ 안에 있으므로, 프로젝트 루트의 bot.py를 불러오려면
# 루트 폴더를 파이썬이 찾는 경로에 먼저 넣어 주어야 한다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from bot import PENDING_PATH, build_item, pending_put, read_pending
except ImportError as e:
    raise SystemExit(
        "[오류] bot.py에서 필요한 함수를 찾지 못했습니다.\n"
        "   bot.py 쪽 이름이 바뀌었을 수 있습니다.\n"
        "   이 파일은 진단용 보조 스크립트이며, 봇 동작과는 상관없습니다.\n"
        f"   개발용 오류 원문: {e}"
    )

# 노션에 이미 있는 옵션값만 씁니다. 새 옵션을 만들지 않습니다.
# 문제위치의 TEST_ 접두는 이 기록이 테스트용임을 노션에서 바로 알아보게 합니다.
TEST_DATA = {
    "과목": "세법",
    "대분류": "부가가치세",
    "단원": "ch07.겸영사업자 세액계산",
    "개념태그": ["공통사용재화"],
    "지식유형": "법령",
    "오답유형": None,
    "교재구분": None,
    "문제위치": "TEST_복구확인",
    "참고출처": None,
    "정리 내용": (
        "재시작 알림 복구 기능을 확인하려고 넣은 테스트 기록입니다.\n"
        "확인이 끝나면 노션에서 이 페이지를 지우셔도 됩니다."
    ),
}


# 이 스크립트는 bot.py의 내부 함수를 직접 부른다. bot.py 쪽 인자가 바뀌면
# 여기가 조용히 깨지는 대신, 무엇이 어긋났는지 한국어로 알리고 끝낸다.
COUPLING_HELP = (
    "[오류] bot.py의 함수 모양이 바뀌어 이 스크립트가 더 이상 맞지 않습니다.\n"
    "   이 파일은 진단용 보조 스크립트이며, 봇 동작과는 상관없습니다.\n"
    "   (봇 자체에 문제가 있는지는 test_flow.py 로 확인하실 수 있습니다.)\n"
    "   맞춰 고칠 곳: add_test_pending.py 의 build_item · pending_put · read_pending 호출"
)


def main():
    try:
        item = build_item(TEST_DATA, None)
        pending_put(item)
        items, _ = read_pending()
    except TypeError as e:
        print(COUPLING_HELP)
        print(f"   개발용 오류 원문: {type(e).__name__}: {e}")
        return 1

    print("[완료] 백업 파일에 테스트용 미저장 기록 1건을 넣었습니다.")
    print(f"   파일: {PENDING_PATH}")
    print(f"   제목: {item['제목']}")
    print(f"   지금 백업 파일에 들어 있는 기록: 총 {len(items)}건")
    print()
    print("다음에 하실 일")
    print("  1) 봇이 켜져 있다면 그 터미널에서 Ctrl+C 로 끕니다.")
    print("  2) 봇을 다시 켭니다:  .venv/bin/python bot.py")
    print("  3) 텔레그램에 '저장하지 못한 기록이 1건 있습니다' 알림이 오는지 봅니다.")
    print("  4) [지금 저장]을 누르면 노션에 저장되고 백업에서 사라집니다.")
    print("     [버리기] → [네, 버립니다]를 누르면 저장하지 않고 지워집니다.")
    print()
    print("※ [지금 저장]으로 노션에 들어간 페이지는 문제위치가 TEST_복구확인 입니다.")
    print("   확인이 끝나면 노션에서 직접 지우시면 됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
