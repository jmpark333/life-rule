#!/usr/bin/env python3
"""GitHub 이슈/코멘트의 /cc 명령을 처리하는 코드 분석 에이전트.

CommandCode API(OpenAI 호환)의 tool calling 으로 저장소를 탐색하고
분석 결과를 이슈 코멘트로 게시한다. `/cc fix` 는 PR 까지 생성한다.

환경변수:
  CMD_API_KEY        CommandCode API 키 (필수)
  GITHUB_TOKEN       GitHub Actions 자동 제공 토큰
  GITHUB_REPOSITORY  owner/repo (Actions 자동 제공)
  ISSUE_NUMBER       이슈 번호
  COMMAND_TEXT       /cc 뒤에 붙은 인자 텍스트
  EVENT_PATH         GitHub 이벤트 payload 경로
  CMD_BASE_URL       기본 https://api.commandcode.ai/provider/v1
  CMD_MODEL          기본 deepseek/deepseek-v4.1-flash
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

API_BASE = os.environ.get("CMD_BASE_URL", "https://api.commandcode.ai/provider/v1")
MODEL = os.environ.get("CMD_MODEL", "deepseek/deepseek-v4.1-flash")
MAX_TOKENS = int(os.environ.get("CMD_MAX_TOKENS", "16000"))
GH_API = "https://api.github.com"
WORKSPACE = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()

MAX_STEPS = 40
MAX_TOOL_OUTPUT = 12_000
MAX_FILE_BYTES = 120_000
MAX_HISTORY_CHARS = 180_000

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", "target", "vendor",
    "__pycache__", ".venv", "venv", ".idea", ".vscode", "coverage", ".cache",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".wasm",
    ".so", ".dll", ".exe", ".class", ".jar", ".bin", ".lock",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "저장소 파일 목록을 조회한다. glob 패턴으로 필터링할 수 있다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "glob": {
                        "type": "string",
                        "description": "예: '*.py', 'src/**/*.ts', '**/Dockerfile'. 생략하면 전체.",
                    },
                    "limit": {"type": "integer", "description": "최대 개수(기본 200)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "파일 전체 또는 특정 줄 범위를 읽는다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "저장소 루트 기준 상대 경로"},
                    "start_line": {"type": "integer", "description": "1부터 시작. 생략 시 처음부터"},
                    "end_line": {"type": "integer", "description": "포함. 생략 시 끝까지"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "정규식으로 저장소 전체를 검색한다. 코드 위치를 빠르게 찾을 때 사용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python 정규식"},
                    "glob": {"type": "string", "description": "검색 대상 파일 패턴(예: '*.py')"},
                    "max_results": {"type": "integer", "description": "기본 60"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "특정 디렉터리의 바로 아래 항목만 조회한다.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "기본값 '.'"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "파일의 일부를 바꾼다. old_text 가 파일 안에서 정확히 한 번만 나와야 한다. "
                           "작은 수정은 이 도구를 쓰고, 새 파일이나 전면 개편만 write_file 을 쓴다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "저장소 루트 기준 상대 경로"},
                    "old_text": {"type": "string", "description": "바꿀 원본 텍스트(공백·줄바꿈 포함, 파일과 완전히 동일해야 함)"},
                    "new_text": {"type": "string", "description": "대체할 텍스트"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "파일 전체를 새 내용으로 저장한다(없으면 생성). 기존 파일을 덮어쓸 때는 "
                           "반드시 read_file 로 먼저 읽어 나머지 내용을 보존해야 한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "저장소 루트 기준 상대 경로"},
                    "content": {"type": "string", "description": "파일 전체 내용"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "파일을 삭제한다. 정말 필요할 때만 사용한다.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]

EDITED: set[str] = set()


class AgentError(RuntimeError):
    pass


# ---------------------------------------------------------------- HTTP helpers

def http_json(method: str, url: str, token: str | None, payload: dict | None = None,
              timeout: int = 120) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "cc-agent")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise AgentError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"{method} {url} -> {exc.reason}") from exc


# ------------------------------------------------------------- path hardening

def safe_path(raw: str) -> Path:
    """저장소 밖으로 나가는 경로를 차단한다."""
    if not raw or raw.strip() == ".":
        return WORKSPACE
    candidate = (WORKSPACE / raw.replace("\\", "/")).resolve()
    if candidate != WORKSPACE and WORKSPACE not in candidate.parents:
        raise AgentError(f"저장소 밖 경로는 접근할 수 없습니다: {raw}")
    return candidate


def is_skipped(rel: PurePosixPath) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts)


# ---------------------------------------------------------------- local tools

def tool_list_files(glob: str | None = None, limit: int = 200) -> str:
    limit = max(1, min(int(limit or 200), 500))
    pattern = glob.strip() if glob else None
    results: list[str] = []
    for path in WORKSPACE.rglob("*"):
        if not path.is_file():
            continue
        rel = PurePosixPath(path.relative_to(WORKSPACE).as_posix())
        if is_skipped(rel):
            continue
        rel_s = str(rel)
        if pattern and not (fnmatch.fnmatch(rel_s, pattern)
                            or fnmatch.fnmatch(rel.name, pattern)
                            or fnmatch.fnmatch(rel_s, pattern.replace("**/", ""))):
            continue
        results.append(rel_s)
        if len(results) >= limit:
            break
    if not results:
        return "(일치하는 파일 없음)"
    head = "\n".join(sorted(results))
    return f"{len(results)}개 파일\n{head}"


def tool_list_dir(path: str = ".") -> str:
    target = safe_path(path or ".")
    if not target.is_dir():
        raise AgentError(f"디렉터리가 아닙니다: {path}")
    entries = []
    for child in sorted(target.iterdir()):
        if child.name in SKIP_DIRS:
            continue
        rel = PurePosixPath(child.relative_to(WORKSPACE).as_posix())
        entries.append(f"{rel}/" if child.is_dir() else str(rel))
    return "\n".join(entries) if entries else "(빈 디렉터리)"


def tool_read_file(path: str, start_line: int | None = None,
                   end_line: int | None = None) -> str:
    target = safe_path(path)
    if not target.is_file():
        raise AgentError(f"파일이 없습니다: {path}")
    rel = PurePosixPath(target.relative_to(WORKSPACE).as_posix())
    if is_skipped(rel) or target.suffix.lower() in BINARY_EXT:
        raise AgentError(f"바이너리/제외 대상 파일입니다: {path}")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise AgentError(f"파일이 너무 큽니다({target.stat().st_size}B). 줄 범위를 지정하세요: {path}")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AgentError(f"UTF-8 로 읽을 수 없습니다: {path}") from exc

    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(start_line or 1))
    end = total if end_line is None else min(total, int(end_line))
    body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
    return f"{rel} ({start}-{end}/{total}줄)\n{body}"


def tool_grep(pattern: str, glob: str | None = None, max_results: int = 60) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise AgentError(f"정규식 오류: {exc}") from exc
    max_results = max(1, min(int(max_results or 60), 200))
    hits: list[str] = []
    for path in WORKSPACE.rglob("*"):
        if not path.is_file():
            continue
        rel = PurePosixPath(path.relative_to(WORKSPACE).as_posix())
        if is_skipped(rel) or path.suffix.lower() in BINARY_EXT:
            continue
        if glob and not (fnmatch.fnmatch(str(rel), glob) or fnmatch.fnmatch(rel.name, glob)):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                if len(hits) >= max_results:
                    return f"{len(hits)}건 (상한 도달)\n" + "\n".join(hits)
    return ("(일치 없음)" if not hits else f"{len(hits)}건\n" + "\n".join(hits))


def rel_of(target: Path) -> str:
    return PurePosixPath(target.relative_to(WORKSPACE).as_posix()).as_posix()


def tool_replace_in_file(path: str, old_text: str, new_text: str) -> str:
    target = safe_path(path)
    if not target.is_file():
        raise AgentError(f"파일이 없습니다: {path}")
    if not old_text:
        raise AgentError("old_text 가 비어 있습니다.")
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        raise AgentError(
            "일치하는 부분이 없습니다. read_file 로 실제 내용을 다시 확인하고 "
            "공백·들여쓰기까지 동일하게 넣어 주세요."
        )
    if count > 1:
        raise AgentError(
            f"{count}곳이 일치해 어느 곳인지 알 수 없습니다. 앞뒤 줄을 포함해 더 길게 지정하세요."
        )
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    rel = rel_of(target)
    EDITED.add(rel)
    return f"수정 완료: {rel}"


def tool_write_file(path: str, content: str) -> str:
    target = safe_path(path)
    if target.suffix.lower() in BINARY_EXT:
        raise AgentError("바이너리 파일은 쓸 수 없습니다.")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise AgentError(f"내용이 너무 큽니다({len(content)}자). 파일을 나누거나 replace_in_file 을 쓰세요.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel = rel_of(target)
    EDITED.add(rel)
    return f"저장 완료: {rel} ({len(content.splitlines())}줄)"


def tool_delete_file(path: str) -> str:
    target = safe_path(path)
    if not target.is_file():
        raise AgentError(f"파일이 없습니다: {path}")
    rel = rel_of(target)
    target.unlink()
    EDITED.add(rel)
    return f"삭제 완료: {rel}"


TOOL_IMPL = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "grep": tool_grep,
    "list_dir": tool_list_dir,
    "replace_in_file": tool_replace_in_file,
    "write_file": tool_write_file,
    "delete_file": tool_delete_file,
}


def run_tool(name: str, args: dict) -> str:
    impl = TOOL_IMPL.get(name)
    if impl is None:
        return f"알 수 없는 도구입니다: {name}"
    try:
        out = impl(**args)
    except AgentError as exc:
        return f"[오류] {exc}"
    except TypeError as exc:
        return f"[인자 오류] {exc}"
    if len(out) > MAX_TOOL_OUTPUT:
        out = out[:MAX_TOOL_OUTPUT] + f"\n... (출력 잘림, 원본 {len(out)}자)"
    return out


# ------------------------------------------------------------------ LLM call

def call_model(messages: list[dict], tools: list[dict]) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.1,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    return http_json("POST", f"{API_BASE}/chat/completions",
                     os.environ["CMD_API_KEY"], payload, timeout=180)


def truncate_history(messages: list[dict]) -> list[dict]:
    total = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    if total <= MAX_HISTORY_CHARS:
        return messages
    keep_head = messages[:2]
    kept: list[dict] = []
    for msg in reversed(messages[2:]):
        size = len(json.dumps(msg, ensure_ascii=False))
        if total <= MAX_HISTORY_CHARS * 0.7:
            break
        total -= size
        kept = [msg] + kept
    return keep_head + [{"role": "system", "content": "이전 도구 출력 일부를 용량 때문에 생략했습니다."}] + kept


def repo_map() -> str:
    """최상위 구조 요약 — 모델이 어디를 고쳐야 하는지 바로 알도록."""
    lines: list[str] = []
    try:
        for child in sorted(WORKSPACE.iterdir()):
            if child.name in SKIP_DIRS or child.name == ".git":
                continue
            if child.name.startswith(".") and child.name != ".github":
                continue
            if child.is_dir():
                names = []
                for sub in sorted(child.iterdir()):
                    if sub.name in SKIP_DIRS:
                        continue
                    names.append(sub.name + ("/" if sub.is_dir() else ""))
                    if len(names) >= 12:
                        names.append("...")
                        break
                lines.append(f"- {child.name}/ : {', '.join(names)}")
            else:
                size = child.stat().st_size
                pages = ""
                try:
                    text = child.read_text(encoding="utf-8")
                    pages = f", {len(text.splitlines())}줄"
                except (UnicodeDecodeError, OSError):
                    pages = ""
                lines.append(f"- {child.name} ({size}B{pages})")
    except OSError:
        return "(구조를 읽지 못했습니다)"
    return chr(10).join(lines) if lines else "(빈 저장소)"


def ui_hint(files: list[str]) -> str:
    """화면 관련 파일을 찾아 '여기를 고쳐야 UI 가 바뀐다'를 짚어 준다."""
    hits = [f for f in files if re.search(r"\.(html?|tsx|jsx|vue|svelte|css|scss)$", f, re.I)]
    return ", ".join(hits[:8]) if hits else "(화면 파일을 찾지 못했습니다. list_files 로 확인하세요.)"


def build_system_prompt(repo: str, issue: dict, fix_mode: bool, file_list: list[str]) -> str:
    if fix_mode:
        mode_lines = [
            "지금은 **수정 모드**입니다. 분석만 하고 끝내지 말고, write_file / replace_in_file 로",
            "저장소 파일을 실제로 고친 뒤 끝냅니다.",
            "- 요청을 **백엔드에서만 구현하고 멈추지 않습니다.** 사용자가 보는 화면(버튼·입력·문구·표시)까지",
            "  같은 작업에서 모두 구현합니다.",
            "- 화면이 바뀌는 요청이면 화면 파일도 반드시 수정합니다. 저장소만 만들고 '준비되었다'고 끝내면 실패입니다.",
            "- 여러 계층이 필요하면 서버(API/저장) → 화면 순서로 끝까지 이어서 구현합니다.",
            "- 작은 수정은 replace_in_file(파일 안에서 정확히 한 번 일치하는 문자열)을 쓰고, 전체 재작성이",
            "  필요할 때만 read_file 로 읽은 뒤 write_file 을 씁니다. 기존 내용을 지어내지 않습니다.",
            "- 수정 후에는 read_file 로 바뀐 부분을 다시 읽어 문법·중괄호·태그가 맞는지 확인합니다.",
            "- 답변(설명)은 자세해도 됩니다. 무엇을 왜 어떻게 바꿨는지 충분히 서술하세요.",
            "- 다만 코드 블록(```)은 넣지 않습니다. 코드는 파일에만 쓰고, 답변에서는 `파일:줄` 인용과 짧은 인라인 코드만 씁니다.",
        ]
    else:
        mode_lines = [
            "지금은 **분석 모드**입니다. 파일을 수정하지 않습니다.",
            "- 사용자가 /cc fix 를 다시 요청하면 바로 구현할 수 있도록, 어느 파일의 어느 부분을",
            "  어떻게 고쳐야 하는지 구체적으로 짚어 줍니다.",
            "- 답변(설명)은 자세해도 됩니다.",
            "- 다만 코드 블록(```)은 넣지 않습니다. `파일:줄` 인용과 짧은 인라인 코드만 씁니다.",
        ]

    head = [
        "당신은 GitHub 저장소 `" + repo + "` 의 코드 에이전트입니다.",
        "",
        "이슈 제목: " + str(issue.get("title", "")),
        "이슈 본문:",
        (issue.get("body") or "(본문 없음)")[:4000],
        "",
        "저장소 최상위 구조:",
        repo_map(),
        "",
        "화면(UI) 관련 파일 후보: " + ui_hint(file_list),
        "",
        "규칙:",
        "- 먼저 도구로 저장소를 충분히 확인한 뒤 판단합니다. 추측을 사실처럼 쓰지 않습니다.",
        "- 파일 인용 시 `경로:줄번호` 형식을 사용합니다.",
        "- 확인하지 못한 내용은 \"확인 필요\"라고 명시합니다.",
        "- 최종 답변은 한국어 존댓말 마크다운으로 3~9줄. 이슈에 코드 전문을 붙이지 않습니다.",
        "- 도구 호출은 최대 " + str(MAX_STEPS) + "회입니다.",
    ]
    return chr(10).join(head + mode_lines) + chr(10)


def extract_fix_plan(text: str) -> dict[str, str]:
    match = re.search(r"```fix_plan\s*\n(.*?)```", text, re.S)
    if not match:
        return {}
    files: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in match.group(1).splitlines():
        if line.startswith("### "):
            if current:
                files[current] = "\n".join(buf).strip("\n") + "\n"
            current = line[4:].strip()
            buf = []
        elif current:
            buf.append(line)
    if current:
        files[current] = "\n".join(buf).strip("\n") + "\n"
    return {k: v for k, v in files.items() if v.strip()}


def run_agent(system_prompt: str, require_fix_plan: bool = False,
              extra_instruction: str = "") -> tuple[str, dict[str, str]]:
    if extra_instruction:
        system_prompt = system_prompt + chr(10) + chr(10) + extra_instruction
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "지금 저장소를 분석하고 결론을 제시해 주세요."},
    ]
    for step in range(MAX_STEPS):
        messages = truncate_history(messages)
        resp = call_model(messages, TOOLS)
        choices = resp.get("choices") or []
        if not choices:
            raise AgentError(f"응답에 choices 가 없습니다: {json.dumps(resp)[:400]}")
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            content = message.get("content") or ""
            if not content.strip():
                raise AgentError("모델이 빈 응답을 반환했습니다.")
            fix_files = extract_fix_plan(content)
            if require_fix_plan and not fix_files:
                # 출력이 잘렸거나 블록을 빠뜨린 경우 한 번만 재요청한다.
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": "답변이 길어 fix_plan 블록이 없습니다. 설명은 3줄로 줄이고 "
                               "```fix_plan 블록에 수정할 파일의 전체 내용을 넣어 다시 출력하세요.",
                })
                retry = call_model(messages, [])
                retry_msg = ((retry.get("choices") or [{}])[0].get("message") or {})
                retry_content = retry_msg.get("content") or ""
                retry_files = extract_fix_plan(retry_content)
                if retry_files:
                    return content + "\n\n---\n\n" + retry_content, retry_files
            return content, fix_files

        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
        })
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(name, args)
            print(f"[tool {step + 1}] {name} {json.dumps(args, ensure_ascii=False)[:160]}", flush=True)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": result,
            })

    # 단계 소진 → 도구 없이 마무리 요청
    messages.append({"role": "user", "content": "도구 호출 한도에 도달했습니다. 지금까지 확인한 내용으로 최종 결론을 작성하세요."})
    resp = call_model(messages, [])
    content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not content.strip():
        raise AgentError("최종 응답을 받지 못했습니다.")
    return content, extract_fix_plan(content)


# ---------------------------------------------------------------- GitHub ops

def post_comment(repo: str, issue_number: int, body: str, token: str) -> None:
    http_json("POST", f"{GH_API}/repos/{repo}/issues/{issue_number}/comments",
              token, {"body": body})


def git_changed() -> tuple[dict[str, str], list[str]]:
    """워크스페이스에서 실제로 바뀐 파일을 모은다(삭제 포함)."""
    proc = subprocess.run(["git", "status", "--porcelain"], cwd=WORKSPACE,
                          capture_output=True, text=True)
    files: dict[str, str] = {}
    deleted: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ")[-1].strip()
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if is_skipped(PurePosixPath(path)):
            continue
        target = WORKSPACE / path
        if target.is_dir():
            for sub in sorted(target.rglob("*")):
                if not sub.is_file() or "__pycache__" in sub.parts:
                    continue
                sub_rel = PurePosixPath(sub.relative_to(WORKSPACE).as_posix()).as_posix()
                try:
                    files[sub_rel] = sub.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
            continue
        if "D" in status or (not status.strip().startswith("??") and not target.is_file()):
            deleted.append(path)
            continue
        if target.suffix.lower() in BINARY_EXT:
            continue
        try:
            files[path] = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return files, deleted


def validate_changes(files: dict[str, str]) -> list[str]:
    notes: list[str] = []
    diff_check = subprocess.run(["git", "diff", "--check"], cwd=WORKSPACE,
                                capture_output=True, text=True)
    if diff_check.returncode != 0:
        notes.append("공백/충돌 표시 문제: " + (diff_check.stdout or diff_check.stderr).strip()[:300])
    py_files = [p for p in files if p.endswith(".py")]
    if py_files:
        proc = subprocess.run([sys.executable, "-m", "py_compile", *py_files], cwd=WORKSPACE,
                              capture_output=True, text=True)
        notes.append("python 문법: " + ("통과" if proc.returncode == 0 else proc.stderr.strip()[:300]))
    pkg = WORKSPACE / "package.json"
    if pkg.is_file() and any(p.endswith((".js", ".html", ".ts")) for p in files):
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {})
        except json.JSONDecodeError:
            scripts = {}
        if "test" in scripts:
            proc = subprocess.run(["npm", "test", "--silent"], cwd=WORKSPACE,
                                  capture_output=True, text=True, timeout=300)
            notes.append("npm test: " + ("통과" if proc.returncode == 0 else proc.stdout.strip()[-300:]))
    return notes


def trim_comment(text: str, limit: int = 6000) -> str:
    """이슈 코멘트는 설명 중심으로 남긴다 — 코드 블록은 PR 에만 둔다."""
    cleaned = re.sub(r"```fix_plan.*?```", "(코드는 PR 에만 반영했습니다)", text, flags=re.S)

    # 설명은 그대로 두고 코드 펜스만 걷어낸다.
    cleaned = re.sub(r"```[^\n]*\n.*?```", "(코드 생략 — PR 참고)", cleaned, flags=re.S)
    cleaned = re.sub(r"```[^\n]*```", "(코드 생략 — PR 참고)", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "\n\n… (설명이 길어 여기서 줄였습니다. 전체 변경은 PR 참고)"
    return cleaned


def create_pr(repo: str, issue_number: int, files: dict[str, str], deletions: list[str],
              base_branch: str, token: str, notes: list[str], summary: str) -> str | None:
    if not files and not deletions:
        return None
    ref = http_json("GET", f"{GH_API}/repos/{repo}/git/ref/heads/{base_branch}", token)
    base_sha = ref["object"]["sha"]
    commit = http_json("GET", f"{GH_API}/repos/{repo}/git/commits/{base_sha}", token)
    base_tree = commit["tree"]["sha"]

    tree_items = []
    for path, content in files.items():
        blob = http_json("POST", f"{GH_API}/repos/{repo}/git/blobs", token,
                         {"content": content, "encoding": "utf-8"})
        tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    for path in deletions:
        tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    new_tree = http_json("POST", f"{GH_API}/repos/{repo}/git/trees", token,
                         {"base_tree": base_tree, "tree": tree_items})
    new_commit = http_json("POST", f"{GH_API}/repos/{repo}/git/commits", token, {
        "message": f"fix: /cc 자동 수정 (issue #{issue_number})",
        "tree": new_tree["sha"],
        "parents": [base_sha],
    })
    branch = f"cc/fix-{issue_number}"
    try:
        http_json("POST", f"{GH_API}/repos/{repo}/git/refs", token,
                  {"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]})
    except AgentError:
        http_json("PATCH", f"{GH_API}/repos/{repo}/git/refs/heads/{branch}", token,
                  {"sha": new_commit["sha"], "force": True})

    changed = [*files.keys(), *[d + " (삭제)" for d in deletions]]
    body_lines = [
        f"이슈 #{issue_number} 의 `/cc fix` 요청으로 생성되었습니다.",
        "",
        "## 변경 파일",
        *[f"- `{c}`" for c in changed],
        "",
        "## 에이전트 요약",
        summary.strip()[:1200] or "(요약 없음)",
    ]
    if notes:
        body_lines += ["", "## 자동 검증"] + [f"- {n}" for n in notes]
    body_lines += ["", "머지 전 반드시 사람이 검토하세요."]

    pr = http_json("POST", f"{GH_API}/repos/{repo}/pulls", token, {
        "title": f"fix: /cc 자동 수정 (issue #{issue_number})",
        "head": branch,
        "base": base_branch,
        "body": "\n".join(body_lines),
    })
    return pr.get("html_url")


# --------------------------------------------------------------------- main

def resolve_event() -> tuple[str, int]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    issue_number = int(os.environ.get("ISSUE_NUMBER", "0") or 0)
    event_path = os.environ.get("EVENT_PATH")

    if event_path and Path(event_path).is_file():
        event = json.loads(Path(event_path).read_text())
        if not repo:
            repo = (event.get("repository") or {}).get("full_name", "")
        if event.get("issue"):
            issue_number = (event["issue"] or {}).get("number", issue_number)

    if not repo or not issue_number:
        raise AgentError("GITHUB_REPOSITORY / ISSUE_NUMBER 를 확인할 수 없습니다.")
    return repo, issue_number


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("::error::GITHUB_TOKEN 이 없습니다.", file=sys.stderr)
        return 1
    if not os.environ.get("CMD_API_KEY"):
        print("::error::CMD_API_KEY 가 없습니다.", file=sys.stderr)
        return 1

    repo, issue_number = resolve_event()
    command_text = (os.environ.get("COMMAND_TEXT") or "").strip()
    fix_mode = bool(re.search(r"\bfix\b", command_text, re.I))

    print(f"::group::cc-agent repo={repo} issue=#{issue_number} model={MODEL}")
    print(f"command: {command_text!r} fix_mode={fix_mode}")

    issue = http_json("GET", f"{GH_API}/repos/{repo}/issues/{issue_number}", token)
    base_branch = (http_json("GET", f"{GH_API}/repos/{repo}", token)).get("default_branch") or "main"
    file_list = [line.strip() for line in tool_list_files(limit=400).splitlines()[1:]]

    system_prompt = build_system_prompt(repo, issue, fix_mode, file_list)

    retry_note = (
        "중요: 이전 시도에서 파일을 하나도 수정하지 않았습니다. 설명으로 끝내지 말고, "
        "지금 바로 write_file 이나 replace_in_file 로 실제 파일을 고치세요. "
        "화면 변경이 필요한 요청이라면 화면 파일도 반드시 포함하세요."
    )
    answer, fix_files = run_agent(system_prompt)
    files, deletions = git_changed()

    if fix_mode and not files and not deletions:
        print("::notice::수정이 없어 한 번 재시도합니다.")
        answer2, fix_files = run_agent(system_prompt, extra_instruction=retry_note)
        if answer2:
            answer = answer2
        files, deletions = git_changed()

    if not files and fix_files:
        files = dict(fix_files)

    body_parts = ["## `/cc` 결과", "", trim_comment(answer)]
    if fix_mode:
        notes = validate_changes(files)
        if files or deletions:
            try:
                pr_url = create_pr(repo, issue_number, files, deletions, base_branch, token,
                                   notes, answer)
                if pr_url:
                    body_parts += ["", f"**PR 생성됨:** {pr_url}"]
                else:
                    body_parts += ["", "**변경 사항이 없어 PR 을 만들지 않았습니다.**"]
            except AgentError as exc:
                body_parts += ["", f"**PR 생성 실패:** {exc}"]
                print(f"::warning::PR 생성 실패: {exc}")
        else:
            body_parts += ["", "**파일이 수정되지 않았습니다.** 요청을 더 구체적으로 적거나 "
                                "`/cc fix` 를 다시 남겨 주세요."]
            if notes:
                body_parts += ["", *[f"- {n}" for n in notes]]

    body_parts += ["", "---", f"<sub>모델: `{MODEL}`</sub>"]
    post_comment(repo, issue_number, "\n".join(body_parts), token)
    print("코멘트 게시 완료")
    print("::endgroup::")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AgentError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)
