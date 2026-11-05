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

MAX_STEPS = 24
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
]


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


TOOL_IMPL = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "grep": tool_grep,
    "list_dir": tool_list_dir,
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


def build_system_prompt(repo: str, issue: dict, fix_mode: bool) -> str:
    mode_rule = (
        "- 사용자가 /cc fix 를 요청했으므로 문제를 실제로 고치는 패치를 제안합니다.\n"
        "  파일 수정은 하지 않습니다. 대신 `fix_plan` 블록에 수정 내용을 적습니다."
        if fix_mode else
        "- 분석과 답변만 합니다. 파일을 수정하거나 파일 수정 계획을 지시하지 않습니다."
    )
    return f"""당신은 GitHub 저장소 `{repo}` 의 코드 분석 에이전트입니다.

이슈 제목: {issue.get('title', '')}
이슈 본문:
{(issue.get('body') or '(본문 없음)')[:4000]}

규칙:
- 먼저 도구로 저장소를 충분히 탐색한 뒤 근거를 들어 답합니다. 추측을 사실처럼 쓰지 않습니다.
- 파일 인용 시 `경로:줄번호` 형식을 사용합니다.
- 확인하지 못한 내용은 "확인 필요"라고 명시합니다.
- 최종 답변은 한국어 존댓말로 작성합니다.
- 최종 답변은 마크다운이며 200~900단어로 간결하게 작성합니다.
- 도구 호출은 최대 {MAX_STEPS}회입니다. 4~10회 안에 결론을 내도록 효율적으로 탐색합니다.
{mode_rule}
- 최종 답변에 ```fix_plan ... ``` 코드블록을 넣으면 자동으로 PR 이 생성됩니다.
  fix_plan 형식(각 항목은 정확한 전체 파일 내용이어야 합니다):
  ```fix_plan
  ### path/to/file.py
  <해당 파일의 전체 새 내용>
  ```
  수정할 파일이 여러 개면 `### 경로` 를 반복합니다.
"""


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


def run_agent(system_prompt: str, require_fix_plan: bool = False) -> tuple[str, dict[str, str]]:
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


def create_pr(repo: str, issue_number: int, files: dict[str, str],
              base_branch: str, token: str) -> str | None:
    if not files:
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

    pr = http_json("POST", f"{GH_API}/repos/{repo}/pulls", token, {
        "title": f"fix: /cc 자동 수정 (issue #{issue_number})",
        "head": branch,
        "base": base_branch,
        "body": f"이슈 #{issue_number} 의 `/cc fix` 요청으로 생성되었습니다.\n\n"
                f"수정 파일: {', '.join(files)}\n\n머지 전 반드시 사람이 검토하세요.",
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

    system_prompt = build_system_prompt(repo, issue, fix_mode)
    answer, fix_files = run_agent(system_prompt, require_fix_plan=fix_mode)

    body = f"## `/cc` 분석 결과\n\n{answer}\n\n---\n<sub>모델: `{MODEL}`</sub>"
    if fix_mode and fix_files:
        try:
            pr_url = create_pr(repo, issue_number, fix_files, base_branch, token)
            if pr_url:
                body += f"\n\n**PR 생성됨:** {pr_url}"
        except AgentError as exc:
            body += f"\n\n**PR 생성 실패:** {exc}"
            print(f"::warning::PR 생성 실패: {exc}")

    post_comment(repo, issue_number, body, token)
    print("코멘트 게시 완료")
    print("::endgroup::")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AgentError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)
