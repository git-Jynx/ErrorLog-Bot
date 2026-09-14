"""content_format.py - 정리 내용 원문 ↔ 노션 블록 변환 (명세서 §13 · §16-4).

저장(파서)과 /export(복원)가 **이 파일 하나의 대응표**를 함께 쓴다.
두 방향을 따로 만들면 "저장은 되는데 내보내면 깨지는" 상태가 된다 (명세서 §13-4).

    원문 ──parse_content()──▶ 노션 블록 ──blocks_to_markdown()──▶ 원문

명세서 §14-1 실측으로 데스크톱 텔레그램이 원문을 훼손하지 않음을 확인했으므로
파서는 `message.text` 하나만 본다. entities를 읽어 복원하는 처리는 없다.
"""

import re

# 노션 공식 문서(Request limits)에서 확인한 제한 수치.
#   - rich_text 한 덩어리(text.content)의 글자 수: 2000자
#   - equation.expression 도 같은 2000자 제한을 쓴다.
TEXT_CHUNK = 2000

# :::code 구분자에 언어를 적지 않으면 이 값으로 본다 (명세서 §13-1).
DEFAULT_LANGUAGE = "text"

# 원문에 적는 언어 이름과 노션 API가 받는 언어 이름이 다른 것만 적는다.
# 노션의 코드 블록 언어 목록에는 `text`가 없고 `plain text`가 있다.
LANGUAGE_TO_NOTION = {"text": "plain text"}
NOTION_TO_LANGUAGE = {"plain text": "text"}

# 수식이 실패했을 때 대신 쓰는 코드 블록의 언어 (명세서 §13-3).
EQUATION_FALLBACK_LANGUAGE = "latex"

LIST_TYPES = ("bulleted_list_item", "numbered_list_item")

# 목록 중첩의 최대 깊이. 0이 맨 바깥이므로 2는 "children이 두 겹"이라는 뜻이다.
# 실측(2026-09-12): 세 겹째 children을 넣으면 노션이 400을 돌려준다.
#   body.children[0]...children[0].bulleted_list_item.children should be not present
# 더 깊이 들여쓴 줄은 이 깊이로 끌어올려 같은 층의 항목으로 넣는다.
MAX_LIST_DEPTH = 2

CODE_FENCE = ":::"
CODE_OPEN_RE = re.compile(r"^:::code(?:\s+(\S+))?\s*$")
BULLET_RE = re.compile(r"^( *)- (.*)$")
NUMBER_RE = re.compile(r"^( *)\d+\. (.*)$")

# 손상 감지용 — "LaTeX 명령어처럼 생긴 것"을 찾는다 (명세서 §15-2 첫째 규칙).
#
# 세 가지 모양을 모두 명령어로 본다. 셋은 같은 하나의 규칙이며, 늘린 것이 아니라
# **역슬래시가 지워진 뒤에도 명령어를 알아보게** 넓힌 것이다 (명세서 §15-3).
#   1) `\frac`  — 역슬래시 + 영문자. 원문 그대로 도착한 경우.
#   2) `\%` `\_` — 역슬래시 + LaTeX 예약문자. §13-5가 쓰라고 정한 표기인데
#      영문자가 없어 1)에 걸리지 않았다.
#   3) `frac{` `text{` — 영문자 뒤에 여는 중괄호. iOS가 역슬래시를 지워도
#      중괄호는 남는다. 이것이 iOS 미감지의 실제 구멍이었다.
LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z]{2,}|\\[%&#_{}$]|[A-Za-z]{2,}\{")


# ============================================================
# 블록 만들기
# ============================================================
def rich_text(content):
    """긴 글은 2000자씩 여러 덩어리로 나눠 담는다. 원문에 없던 줄바꿈을 만들지 않는다."""
    if not content:
        return []
    return [
        {"type": "text", "text": {"content": content[i:i + TEXT_CHUNK]}}
        for i in range(0, len(content), TEXT_CHUNK)
    ]


def _block(kind, payload):
    return {"object": "block", "type": kind, kind: payload}


def paragraph_block(text):
    return _block("paragraph", {"rich_text": rich_text(text)})


def heading_block(text):
    return _block("heading_2", {"rich_text": rich_text(text)})


def equation_block(expression):
    return _block("equation", {"expression": expression})


def code_block(text, language):
    notion_language = LANGUAGE_TO_NOTION.get(language, language)
    return _block("code", {"rich_text": rich_text(text), "language": notion_language})


def list_block(kind, text):
    return _block(kind, {"rich_text": rich_text(text)})


def plain_text_of(block):
    """블록 하나의 글자를 잇는다.

    우리가 만든 블록은 `text.content`를, 노션이 돌려준 블록은 `plain_text`를 갖는다.
    /export가 노션에서 읽어 온 블록을 그대로 다루어야 하므로 둘 다 받는다.
    """
    payload = block.get(block.get("type"), {})
    pieces = []
    for item in payload.get("rich_text", []):
        if "plain_text" in item:
            pieces.append(item["plain_text"])
        else:
            pieces.append(item.get("text", {}).get("content", ""))
    return "".join(pieces)


# ============================================================
# 원문 → 노션 블록 (명세서 §13-1)
# ============================================================
def parse_content(text):
    """정리 내용 원문을 노션 블록 목록으로 바꾼다.

    | 입력                  | 블록                                      |
    | --------------------- | ----------------------------------------- |
    | `$$` ~ `$$`           | equation (한 줄 `$$ ... $$` 형태도 처리)  |
    | `:::code <언어>` ~ `:::` | code                                   |
    | `## `                 | heading_2                                 |
    | `- `                  | bulleted_list_item (공백 2칸마다 children) |
    | `1. `                 | numbered_list_item                        |
    | 그 외                 | paragraph                                 |
    | 빈 줄                 | 구분자로만 쓰고 빈 paragraph를 만들지 않음 |

    굵게·기울임·인용·표·인라인 수식은 입력 규격에서 금지되어 있으므로
    처리하지 않는다 (명세서 §13-2). 규칙을 늘리면 오탐이 늘어난다.
    """
    lines = text.split("\n")
    blocks = []
    # 목록 중첩용. [(들여쓰기 깊이, 그 깊이의 마지막 블록)]
    stack = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 빈 줄 — 블록 구분자로만 쓴다. 목록의 연결도 여기서 끊긴다.
        if not stripped:
            stack = []
            i += 1
            continue

        # 코드 블록이 가장 먼저다. 안에 든 `## `나 `- `를 서식으로 보면 안 된다.
        opened = CODE_OPEN_RE.match(stripped)
        if opened:
            stack = []
            language = (opened.group(1) or DEFAULT_LANGUAGE).lower()
            i += 1
            body = []
            while i < len(lines) and lines[i].strip() != CODE_FENCE:
                body.append(lines[i])
                i += 1
            i += 1  # 닫는 `:::`. 없이 끝나면 그냥 문서 끝이다.
            blocks.append(code_block("\n".join(body), language))
            continue

        if stripped.startswith("$$"):
            stack = []
            rest = stripped[2:]
            if rest.endswith("$$") and rest[:-2].strip():
                # 한 줄짜리 `$$ ... $$`
                expression = rest[:-2].strip()
                i += 1
            else:
                parts = [rest] if rest.strip() else []
                i += 1
                while i < len(lines) and lines[i].strip() != "$$":
                    parts.append(lines[i])
                    i += 1
                i += 1  # 닫는 `$$`
                expression = "\n".join(parts).strip()
            blocks.append(equation_block(expression))
            continue

        if line.startswith("## "):
            stack = []
            blocks.append(heading_block(line[3:].strip()))
            i += 1
            continue

        bullet = BULLET_RE.match(line)
        number = None if bullet else NUMBER_RE.match(line)
        if bullet or number:
            matched = bullet or number
            kind = "bulleted_list_item" if bullet else "numbered_list_item"
            depth = min(len(matched.group(1)) // 2, MAX_LIST_DEPTH)
            block = list_block(kind, matched.group(2).strip())
            _attach(blocks, stack, depth, block)
            i += 1
            continue

        stack = []
        blocks.append(paragraph_block(line))
        i += 1

    return blocks


def _attach(blocks, stack, depth, block):
    """들여쓴 목록 항목을 위 항목의 children으로 넣는다 (명세서 §13-1)."""
    while stack and stack[-1][0] >= depth:
        stack.pop()
    if stack:
        parent = stack[-1][1]
        parent[parent["type"]].setdefault("children", []).append(block)
    else:
        blocks.append(block)
    stack.append((depth, block))


# ============================================================
# 노션 블록 → 마크다운 (명세서 §16-4) — 위 표의 역방향
# ============================================================
def blocks_to_markdown(blocks):
    """노션 블록을 원문 마크다운으로 되돌린다. parse_content()의 역방향이다.

    블록 사이에는 빈 줄을 하나 넣되, **같은 종류의 목록 항목끼리는 넣지 않는다.**
    그래야 내보낸 파일을 그대로 다시 붙여넣었을 때 같은 블록이 만들어지고,
    글머리 목록 뒤에 번호 목록이 이어질 때도 원문과 같은 모양이 된다.
    """
    return "\n".join(_render(blocks, 0))


def _render(blocks, depth):
    out = []
    previous = None
    counter = 0
    pad = "  " * depth

    for block in blocks:
        kind = block.get("type")
        if out and not (previous == kind and kind in LIST_TYPES):
            out.append("")

        if kind == "heading_2":
            out.append("## " + plain_text_of(block))
        elif kind == "equation":
            out.append("$$")
            out.extend((block["equation"].get("expression") or "").split("\n"))
            out.append("$$")
        elif kind == "code":
            language = block["code"].get("language") or DEFAULT_LANGUAGE
            out.append(":::code " + NOTION_TO_LANGUAGE.get(language, language))
            out.extend(plain_text_of(block).split("\n"))
            out.append(CODE_FENCE)
        elif kind == "bulleted_list_item":
            out.append(pad + "- " + plain_text_of(block))
            out.extend(_render(_children_of(block), depth + 1))
        elif kind == "numbered_list_item":
            counter = counter + 1 if previous == "numbered_list_item" else 1
            out.append(pad + f"{counter}. " + plain_text_of(block))
            out.extend(_render(_children_of(block), depth + 1))
        else:
            # paragraph, 그리고 사람이 노션에서 직접 넣은 그 밖의 글 블록.
            # 글자가 있으면 문단으로 내보내고, 글자가 없는 블록(구분선·이미지 등)은 건너뛴다.
            text = plain_text_of(block)
            if text:
                out.append(text)
            elif out and out[-1] == "":
                out.pop()
                previous = kind
                continue
            else:
                previous = kind
                continue

        previous = kind

    return out


def _children_of(block):
    payload = block.get(block.get("type"), {})
    return payload.get("children") or block.get("children") or []


# ============================================================
# 수식 실패 폴백 (명세서 §13-3)
# ============================================================
def replace_equations(blocks, predicate):
    """조건에 맞는 equation 블록을 code 블록으로 바꾼다.

    (바꾼 블록 목록, 바꾼 수식의 번호 목록)을 돌려준다. 번호는 문서에 나온
    순서대로 1부터 센다. 원본 목록은 건드리지 않는다.
    """
    changed = []
    counter = [0]

    def walk(items):
        result = []
        for block in items:
            if block.get("type") == "equation":
                counter[0] += 1
                if predicate(block):
                    changed.append(counter[0])
                    expression = block["equation"].get("expression") or ""
                    result.append(code_block(expression, EQUATION_FALLBACK_LANGUAGE))
                    continue
            copied = dict(block)
            children = _children_of(block)
            if children:
                payload = dict(copied[copied["type"]])
                payload["children"] = walk(children)
                copied[copied["type"]] = payload
            result.append(copied)
        return result

    return walk(blocks), changed


def is_unusable_equation(block):
    """노션이 빈 블록으로 만들거나 아예 거부할 수식인지 미리 본다.

    빈 수식은 노션에서 아무것도 없는 블록이 되어 내용이 사라지고,
    2000자를 넘는 수식은 노션이 요청 자체를 거부한다.
    """
    expression = block["equation"].get("expression") or ""
    return not expression.strip() or len(expression) > TEXT_CHUNK


def equation_notice(numbers):
    """폴백을 알리는 한 줄 (명세서 §13-3)."""
    if not numbers:
        return None
    listed = "·".join(str(n) for n in numbers)
    return f"⚠️ 수식 {listed}번이 렌더되지 않아 코드블록으로 저장했습니다."


# ============================================================
# 손상 감지 안전망 (명세서 §15-2)
# ============================================================
def strip_code_blocks(text):
    """`:::code` ~ `:::` 구간을 들어낸 글을 돌려준다.

    LaTeX 검사에만 쓴다. 코드블록에는 윈도우 경로(`C:\\Users\\...`)나
    `main() {` 같은 글이 들어올 수 있어, 그것을 수식 명령어로 오해하면
    멀쩡한 원문이 경고에 걸린다. 규칙을 늘리는 것이 아니라
    **기존 규칙이 보는 범위를 좁히는 것**이다.

    구간을 가르는 방법은 parse_content()와 같아야 한다. 다르면
    "검사는 통과했는데 저장은 다르게 되는" 상태가 된다.
    """
    kept = []
    inside = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not inside:
            if CODE_OPEN_RE.match(stripped):
                inside = True
                continue
            kept.append(line)
        elif stripped == CODE_FENCE:
            inside = False
    return "\n".join(kept)


def detect_corruption(text):
    """모바일 전송으로 서식 기호가 소실된 정황을 찾는다. 없으면 None.

    완전 탐지는 불가능하다. 가장 흔한 사고만 막는 것이 목적이므로
    규칙을 더 늘리지 않는다 (명세서 §15-2). 규칙은 아래 세 개 그대로다.

    첫째 규칙은 코드블록 **바깥**만 본다. `:::` 개수와 `$$` 개수는 글 전체를
    센다 — 구분자가 짝이 맞는지 보는 규칙이라 안팎을 가를 수 없다.
    """
    if LATEX_COMMAND_RE.search(strip_code_blocks(text)) and text.count("$$") == 0:
        return "수식 명령어가 있는데 `$$` 구분자가 하나도 없습니다."
    if text.count(CODE_FENCE) % 2 == 1:
        return "코드블록 구분자 `:::`의 개수가 홀수입니다."
    if text.count("$$") % 2 == 1:
        return "수식 구분자 `$$`의 개수가 홀수입니다."
    return None
