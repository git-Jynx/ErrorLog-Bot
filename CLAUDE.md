# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Scope:** Only what current models still get wrong. If the model or the harness already handles something reliably, it doesn't belong here - a rule that restates default behavior burns context and buys nothing.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. State Assumptions, Then Proceed

**Say what you assumed. Keep going. Default the rest.**

Before implementing:
- State your assumptions in one line, then start.
- If multiple interpretations exist, pick the likeliest and say which one you picked.
- If a simpler approach exists, say so while doing the work - not as a question that blocks it.
- Ask only when the answer changes what gets built, not how well, and the wrong choice can't be cheaply undone.

A stated assumption gets corrected in seconds. A question costs a round-trip and hands the work back to the user. If you're about to ask a second question in one task, you're doing it wrong.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Verify Before Done

**If you touched code, run the check before saying "done" - and report what actually ran.**

- `npm test`, `pytest`, `cargo test`, whatever the project uses. Smallest relevant check first, broader checks when risk is high.
- No test setup? At minimum, verify the project builds or typechecks.
- Report the exact command and its result: "passed", "failed with X", or "not run because Y".
- Never write "done", "fixed", or "works" unless a concrete check backs it.
- Run it proactively, before the user signals "끝", "완료", "다 됐어".

This is the step LLMs skip most often. Treat it as non-negotiable.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and stated assumptions get corrected early instead of surfacing as mistakes late.

---

## 5. [User-Specific] Beginner-Friendly Communication
- The user is a beginner in programming. Always explain technical concepts in easy-to-understand Korean.
- When suggesting a terminal command, provide the EXACT command to copy and paste (e.g., `pip install -r requirements.txt`).
- Do not skip steps in setup instructions (like how to create a virtual environment).

## 6. [User-Specific] Strict Security (Always hide keys)
- NEVER hardcode API keys, Tokens, or DB IDs in the code.
- Always use a `.env` file and `python-dotenv` (or equivalent) for ANY project involving credentials.
- Always generate a `.env.example` and ensure `.env` is inside `.gitignore` **before the first commit that touches credentials** - not after.
- If a real secret is ever committed to git (even briefly, even in a local commit that hasn't been pushed), treat it as compromised: deleting the file or amending the commit is NOT enough because git history retains it. Tell the user explicitly to revoke/rotate that key at the source (Telegram BotFather, Notion integrations page, Google AI Studio, etc.) before continuing.

## 7. [User-Specific] GitHub 배포 전제
Every project this user builds is eventually pushed to a public GitHub repository. Behave accordingly from the first commit, not retroactively:
- Create a `.gitignore` appropriate to the stack (`.env`, `__pycache__/`, `.venv/`, `node_modules/`, `*.db`, OS/editor junk) as one of the first files in the project, before writing code that touches credentials or generates local data.
- Default to an MIT license if the user hasn't specified one and it becomes relevant (e.g., they ask you to init the repo) - state this as an assumption per Rule 1, don't block on it.
- Don't commit or suggest committing large binaries, virtual environments, or generated data files.

## 8. [User-Specific] 문서화 범위 분리
README 작성은 이 파일의 범위가 아니다. 프로젝트가 완료된 후, 별도의 `README_BLUEPRINT.md` 지침서를 사용해 사용자용 `README.md`를 따로 생성한다. 개발 중에는 README나 사용 설명서를 자발적으로 작성하지 말고, 요청받았을 때만 `README_BLUEPRINT.md`를 참조해 작성한다.
