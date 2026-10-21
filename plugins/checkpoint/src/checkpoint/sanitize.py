"""Scrubbing before anything reaches the journal (PLAN-v4 phase 2).

Everything checkpoint journals passes through here first: prompt text,
commands, tool inputs and outputs, notes, subagent messages. The journal is a
record for recovery, not a log of values, so this errs on the side of
masking: a masked count or path costs little, a stored token costs a lot.

- `scrub(text)`: token shapes (GitHub ghp_/gho_/ghu_/ghs_/ghr_/github_pat_,
  GitLab glpat-, Hugging Face hf_, OpenAI/Anthropic sk-..., Stripe sk_/rk_,
  Slack xox?-, AWS AKIA/ASIA key ids and secret keys, Google AIza..., npm_,
  pypi-, age secret keys, JWTs), PEM private key blocks, URLs with
  user:pass@, Authorization/Bearer headers, `--password x`-style flags, and
  `*KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*` assignments in shell, env, YAML
  and JSON text (names with separators, camelCase, or none: PGPASSWORD,
  APITOKEN; a value up to the next space, commas included), each replaced
  with `***` (the name is kept, the value goes).
- `scrub_value(obj)`: the same inside a JSON-like value, and any dict field
  whose name says key/token/secret/password/credential/auth/cookie loses its
  value whatever it looks like.
- `tool_input(tool, input)` / `tool_response(tool, response)`: bounded,
  scrubbed summaries. Known tools keep what recovery needs (the command, the
  file path, the plan step); file contents and edit strings are never kept
  (their size and hash are), neither are Read, Grep, WebFetch or WebSearch
  results (paths, counts and sizes are), nor the output of a command that
  isn't a test, build or check (its size is); an unknown tool (an MCP tool,
  say) keeps a short scrubbed summary of its input and only the keys and size
  of its result.
- `clip(text, n)`: text cut to n characters with a marker.

Patterns, not a vault: checkpoint holds no secrets of its own to compare
against (the gateway owns those and scrubs its own output first).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MASK = "***"

# Token shapes. Each is replaced whole.
_SHAPES = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
               r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S),
    re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{10,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{10,}"),
    re.compile(r"\bsk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}"),
    re.compile(r"\bpypi-[A-Za-z0-9_-]{30,}"),
    re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
]

# The words that make a name sensitive (a field, a variable, a flag).
_WORDS = ("key", "keys", "token", "secret", "secrets", "password", "passwords", "passwd", "pwd", "pass",
          "passphrase", "credential", "credentials", "cred", "creds", "auth", "authorization",
          "cookie", "cookies", "apikey", "privatekey", "accesskey", "secretkey", "sessiontoken",
          "bearer", "pat", "otp", "pin", "signature", "sig")
_NAME_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
# Inside a name written without separators (PGPASSWORD, APITOKEN, GITHUBTOKEN,
# pgpassword): these anywhere make it sensitive. "token" followed by "s" or
# "iz"/"is" is a count or a tokenizer (max_tokens, input_tokens, tokenizer).
_INNER = re.compile(r"password|passwd|passphrase|secret|apikey|accesskey|privatekey|token(?!s|iz|is)")
HOST_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")   # a gateway host name (its form validates them)


def sensitive_name(name: str) -> bool:
    """Does a field or variable name say it holds a secret? Split into words
    (snake_case, kebab-case, dotted, camelCase): any word in _WORDS; or, for
    names written without separators, one of _INNER anywhere in it (PGPASSWORD,
    APITOKEN, DBPASSWORD). Counts (`max_tokens`, `input_tokens`) are not."""
    if not name:
        return False
    words = [w.lower() for w in _NAME_SPLIT.split(name) if w]
    joined = "".join(words)
    return any(w in _WORDS for w in words) or joined in _WORDS or bool(_INNER.search(joined))


_IDENT = r"[A-Za-z_][A-Za-z0-9_.-]*"
# NAME=value / NAME: value / export NAME=value, where NAME is sensitive (checked
# in the callback). The value: a quoted string or a run without spaces (commas
# included: `PASSWORD=ab,cd` is one value; a value that isn't sensitive is
# searched again, so `a=1,TOKEN=x` and `a=1:TOKEN=x` still lose x).
_ASSIGN = re.compile(r"(?P<name>" + _IDENT + r")(?P<sep>\s*[=:]\s*)"
                     r"(?P<val>\"(?:\\.|[^\"\\])*\"|'[^']*'|[^\s;&|)}\]]+)")
# "name": "value" in JSON text (escaped quotes inside the value handled).
_JSON_FIELD = re.compile(r"(?P<q>\\?\")(?P<name>[^\"\\]{1,80})(?P=q)(?P<sep>\s*:\s*)"
                         r"(?P<val>(?P=q)(?:\\.|[^\"\\])*?(?P=q)|-?\d[\d.]*|true|false)")
# --password VALUE / --token=VALUE / -p VALUE is too ambiguous to touch.
_FLAG = re.compile(r"(?P<flag>--?(?P<name>[A-Za-z][A-Za-z0-9_-]*))(?P<sep>=|\s+)(?P<val>\"[^\"]*\"|'[^']*'|[^\s-][^\s]*)")
_URL_CRED = re.compile(r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^/\s:@'\"]+):(?P<pw>[^/\s@'\"]+)@")
_AUTH = re.compile(r"(?i)\b(?P<h>(?:proxy-)?authorization\s*[:=]\s*(?:(?:bearer|basic|token|digest|negotiate)\s+)?)"
                   r"[^\s\"',;]+")
_BEARER = re.compile(r"(?i)\b(?P<h>bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
# "token abc123...", "password is Xy9...": a sensitive word, then a value that
# looks generated (12+ characters of letters with digits or symbols).
_WORD_VALUE = re.compile(r"(?i:(?P<w>(?:\b(?:api[ _-]?)?(?:key|pat|credential)"
                         r"|\b[A-Za-z0-9_]*?(?:token|secret|password|passwd|passphrase|apikey|accesskey|privatekey))s?)"
                         r"(?P<sep>\s+(?:is\s+|was\s+|=\s*|:\s*)?))"
                         r"(?P<val>(?=[A-Za-z0-9_./+=-]*\d)(?=[A-Za-z0-9_./+=-]*[A-Za-z])[A-Za-z0-9_./+=-]{12,}"
                         r"|(?=[A-Za-z]*[a-z])(?=[A-Za-z]*[A-Z][A-Za-z]*[A-Z])[A-Za-z]{20,})\b")
# A 40-character AWS secret key near a word that says so.
_AWS_SECRET = re.compile(r"(?i)(?P<pre>aws.{0,20}?(?:secret|access).{0,20}?[=:\s]\s*[\"']?)[A-Za-z0-9/+=]{40}\b")


_SCHEMES = {"bearer", "basic", "token", "digest", "negotiate", MASK}


_CHAINED = re.compile(_IDENT + r"[=:]?")
_SEP_NEXT = re.compile(r"\s*[=:]")


def _assign_all(text: str, depth: int = 0) -> str:
    """Every sensitive NAME=value in text masked. A value whose name isn't
    sensitive is searched again (an assignment can hide inside it: `a=1:TOKEN=x`,
    `opts=user,password=x`, `cmd="PGPASSWORD=x psql"`); past a few levels a
    value that still holds an assignment is masked whole. A value that is
    itself the next name (`note: PGPASSWORD: x`, `config: DB_PASSWORD = x`)
    is read again as a name: the scan goes on from it."""
    out: list[str] = []
    pos = 0
    while True:
        m = _ASSIGN.search(text, pos)
        if not m:
            break
        name, sep, val = m.group("name"), m.group("sep"), m.group("val")
        if not sensitive_name(name):
            if _CHAINED.fullmatch(val) and (val[-1] in "=:" or _SEP_NEXT.match(text, m.end())):
                out.append(text[pos:m.start("val")])
                pos = m.start("val")
                continue
            if depth >= 6:
                rep = f"{name}{sep}{MASK}" if _ASSIGN.search(val) else m.group(0)
            else:
                rep = f"{name}{sep}{_assign_all(val, depth + 1)}"
        elif val.lower() in _SCHEMES:   # `Authorization: Basic ***` (the credential is masked already)
            rep = m.group(0)
        elif val.startswith(("\"", "'")):
            rep = f"{name}{sep}{val[0]}{MASK}{val[0]}"
        else:
            rep = f"{name}{sep}{MASK}"
        out.append(text[pos:m.start()])
        out.append(rep)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _json_sub(m: re.Match) -> str:
    if not sensitive_name(m.group("name")):
        return m.group(0)
    q = m.group("q")
    return f"{q}{m.group('name')}{q}{m.group('sep')}{q}{MASK}{q}"


def _flag_sub(m: re.Match) -> str:
    if not sensitive_name(m.group("name")):
        return m.group(0)
    return f"{m.group('flag')}{m.group('sep')}{MASK}"


def scrub(text: Any) -> str:
    """Text with every secret shape masked (see the module docstring)."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    for rx in _SHAPES:
        text = rx.sub(MASK, text)
    text = _URL_CRED.sub(lambda m: f"{m.group('pre')}{m.group('user')}:{MASK}@", text)
    text = _AUTH.sub(lambda m: m.group("h") + MASK, text)
    text = _BEARER.sub(lambda m: m.group("h") + MASK, text)
    text = _AWS_SECRET.sub(lambda m: m.group("pre") + MASK, text)
    text = _WORD_VALUE.sub(lambda m: m.group("w") + m.group("sep") + MASK, text)
    text = _JSON_FIELD.sub(_json_sub, text)
    text = _FLAG.sub(_flag_sub, text)
    text = _assign_all(text)
    return text


def scrub_value(obj: Any, depth: int = 0) -> Any:
    """A JSON-like value with sensitive fields emptied and strings scrubbed."""
    if depth > 12:
        return MASK
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:200]:
            k = str(k)
            if sensitive_name(k) and v not in (None, "", [], {}) and not isinstance(v, bool):
                out[k] = MASK
            else:
                out[k] = scrub_value(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [scrub_value(v, depth + 1) for v in obj[:200]]
    if isinstance(obj, str):
        return scrub(obj)
    return obj


def clip(text: Any, n: int) -> str:
    """At most n characters (a marker says how much was cut)."""
    text = "" if text is None else str(text)
    if len(text) <= n:
        return text
    keep = max(0, n - 24)
    return text[:keep] + f" ...(+{len(text) - keep} chars)"


def clip_path(text: Any, n: int) -> str:
    """A path in at most n characters, cut in the middle so its file name
    stays (`/private/tmp/...(+87 chars).../m.py`); a file name longer than
    that is clipped like any text."""
    text = "" if text is None else str(text)
    if len(text) <= n:
        return text
    base = text.rsplit("/", 1)[-1]
    room = n - len(base) - len(f"...(+{len(text)} chars).../")
    if "/" not in text or room < 8:
        return clip(text, n)
    head = text[:room]
    return f"{head}...(+{len(text) - len(head) - len(base) - 1} chars).../{base}"


def tail(text: Any, n: int) -> str:
    """The last n characters (a marker says how much was cut)."""
    text = "" if text is None else str(text)
    if len(text) <= n:
        return text
    keep = max(0, n - 24)
    return f"...({len(text) - keep} chars cut) " + text[-keep:]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _s(v: Any, n: int) -> str:
    return clip(scrub(v), n)


def _url(u: Any) -> str:
    """A URL without its query string and fragment (they carry tokens)."""
    u = scrub(u or "")
    return clip(re.sub(r"[?#].*$", "", u) + (" (query dropped)" if re.search(r"[?#]", u) else ""), 400)


def tool_input(tool: str, inp: Any) -> dict[str, Any]:
    """What the journal keeps of a tool call's input: bounded and scrubbed.
    File contents and edit strings are never kept, only their sizes (and for
    Write a hash of the new content)."""
    if not isinstance(inp, dict):
        return {"summary": _s(json.dumps(inp, default=str), 200)} if inp not in (None, "") else {}
    g = inp.get
    if tool == "Bash":
        out = {"command": _s(g("command"), 2000)}
        if g("description"):
            out["description"] = _s(g("description"), 200)
        if g("run_in_background"):
            out["background"] = True
        if g("timeout"):
            out["timeout"] = g("timeout") if isinstance(g("timeout"), (int, float)) else _s(g("timeout"), 20)
        return out
    if tool in ("Write", "Edit", "MultiEdit"):
        out = {"file_path": _s(g("file_path"), 500)}
        if tool == "Write" and isinstance(g("content"), str):
            out["content_bytes"] = len(g("content").encode("utf-8", "surrogatepass"))
            out["content_sha256"] = sha256_text(g("content"))
        if tool == "Edit":
            out["old_len"] = len(g("old_string") or "")
            out["new_len"] = len(g("new_string") or "")
            if g("replace_all"):
                out["replace_all"] = True
        if tool == "MultiEdit" and isinstance(g("edits"), list):
            out["edits"] = len(g("edits"))
        return out
    if tool == "NotebookEdit":
        return {k: _s(g(k), 300) for k in ("notebook_path", "cell_id", "edit_mode", "cell_type") if g(k)}
    if tool in ("Read", "Glob", "Grep", "LS"):
        return {k: (_s(g(k), 300) if isinstance(g(k), str) else g(k))
                for k in ("file_path", "path", "pattern", "glob", "offset", "limit", "type")
                if g(k) not in (None, "") and not isinstance(g(k), (dict, list))}
    if tool == "TaskCreate":
        return {k: _s(g(k), 500 if k == "description" else 200)
                for k in ("subject", "description", "activeForm") if g(k)}
    if tool == "TaskUpdate":
        out = {k: _s(g(k), 500 if k == "description" else 200)
               for k in ("taskId", "status", "subject", "description", "activeForm", "owner") if g(k)}
        for k in ("addBlockedBy", "addBlocks"):
            if isinstance(g(k), list):
                out[k] = [_s(x, 40) for x in g(k)[:20]]
        return out
    if tool == "TodoWrite":
        todos = g("todos") if isinstance(g("todos"), list) else []
        return {"todos": [{"content": _s(t.get("content"), 200), "status": _s(t.get("status"), 20),
                           **({"id": _s(t.get("id"), 40)} if t.get("id") else {})}
                          for t in todos[:50] if isinstance(t, dict)],
                **({"cut": len(todos) - 50} if len(todos) > 50 else {})}
    if tool in ("Agent", "Task"):
        out = {k: _s(g(k), 600 if k == "prompt" else 200)
               for k in ("description", "subagent_type", "prompt", "name") if g(k)}
        if g("run_in_background"):
            out["background"] = True
        return out
    if tool == "WebFetch":
        return {"url": _url(g("url")), **({"prompt": _s(g("prompt"), 200)} if g("prompt") else {})}
    if tool == "WebSearch":
        return {"query": _s(g("query"), 200)}
    if tool == "Skill":
        return {k: _s(g(k), 200) for k in ("skill", "args") if g(k)}
    if tool.endswith("__job_start"):
        out = {k: _s(g(k), 1000 if k == "command" else 200)
               for k in ("name", "command", "cwd", "task", "redo_hint") if g(k)}
        # the host it runs on when that is a plain name (the gateway's job_start;
        # it runs on localhost when the call names none): hosts.call_hosts
        h = g("host")
        if isinstance(h, str) and HOST_NAME_RE.fullmatch(h) and scrub(h) == h:
            out["host"] = h
        elif h in (None, "") and tool == "mcp__plugin_gate_gw__job_start":
            out["host"] = "localhost"
        return out
    if tool.endswith("__checkpoint_note"):
        return {k: _s(g(k), 1000) for k in ("kind", "text") if g(k)}
    # Anything else (MCP tools, new built-ins): a short scrubbed summary only,
    # and the host or account it names when that is a plain name (the
    # gateway's terminal_open, job_start, http ...: which host or account the
    # task used).
    keys = sorted(str(k) for k in inp.keys())[:20]
    out = {"keys": keys, "summary": clip(json.dumps(scrub_value(inp), default=str, ensure_ascii=False), 300)}
    for k in ("host", "account"):     # the gateway's http/download tools name an account
        h = g(k)
        if isinstance(h, str) and HOST_NAME_RE.fullmatch(h) and scrub(h) == h:
            out[k] = h
    return out


EXIT_RE = re.compile(r"^\s*Exit code (-?\d+)")


# A shell result that says its command went on in the background (Claude Code
# 2.1.301: run_in_background, Ctrl+B, a turn aborted, a message delivered, a
# timeout): which of these it was.
_BG_WHY = (("backgroundedByUser", "user"), ("backgroundedByTurnAbort", "turn_abort"),
           ("backgroundedToDeliverMessage", "deliver_message"))
# An Agent result for a subagent that goes on after the call returns.
AGENT_LAUNCHED = ("async_launched", "remote_launched", "teammate_spawned")


def _num(v: Any) -> int | float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def stated_exit(resp: Any) -> int | None:
    """The exit code a tool result states, if it states one (Claude Code's
    Bash result doesn't: a non-zero exit it counts as an error comes as
    PostToolUseFailure, "Exit code N")."""
    if isinstance(resp, dict):
        for k in ("exitCode", "exit_code", "code"):
            v = resp.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
    return None


def background_of(tool: str, resp: Any, inp: Any = None) -> str:
    """'' when the call's result is its outcome; else why it went on in the
    background (the result only says it started): a Bash command with a
    backgroundTaskId (or run_in_background), an Agent launched async."""
    if isinstance(resp, dict):
        if isinstance(resp.get("backgroundTaskId"), str) and resp["backgroundTaskId"]:
            if resp.get("timedOutAfterMs"):
                return "timeout"
            return next((why for k, why in _BG_WHY if resp.get(k)), "background")
        if tool in ("Agent", "Task") and (resp.get("isAsync") is True or resp.get("status") in AGENT_LAUNCHED):
            return str(resp.get("status") or "async_launched")
    if isinstance(inp, dict) and inp.get("run_in_background") is True and tool in ("Bash", "Agent", "Task"):
        return "background"
    return ""


def tool_response(tool: str, resp: Any, output: bool = False) -> dict[str, Any]:
    """What the journal keeps of a tool's result: bounded and scrubbed. Never
    file contents: Read, Grep, Glob, WebFetch and WebSearch keep paths, counts
    and sizes; a Bash command keeps the tails of its output only with `output`
    (a test, build or check: evidence), else their sizes; an unknown tool keeps
    the result's keys and size."""
    if tool == "Bash":
        if isinstance(resp, dict):
            out: dict[str, Any] = {}
            if resp.get("interrupted"):
                out["interrupted"] = True
            ex = stated_exit(resp)
            if ex is not None:
                out["exit"] = ex
            if resp.get("returnCodeInterpretation"):
                out["interpretation"] = _s(resp["returnCodeInterpretation"], 200)
            for k in ("stdout", "stderr"):
                if resp.get(k):
                    if output:
                        out[k + "_tail"] = tail(scrub(resp[k]), 400)
                    else:
                        out[k + "_chars"] = len(str(resp[k]))
            if resp.get("backgroundTaskId"):
                out["background_id"] = _s(resp["backgroundTaskId"], 60)
                why = background_of(tool, resp)
                if why != "background":
                    out["background_why"] = why
                if _num(resp.get("timedOutAfterMs")) is not None:
                    out["timed_out_ms"] = resp["timedOutAfterMs"]
            return out
        if not resp:
            return {}
        return {"text_tail": tail(scrub(resp), 400)} if output else {"text_chars": len(str(resp))}
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        if isinstance(resp, dict):
            return {k: resp[k] for k in ("type", "userModified", "replaceAll") if k in resp
                    and isinstance(resp[k], (bool, str)) and len(str(resp[k])) < 40}
        return {}
    if tool == "TaskCreate" and isinstance(resp, dict):
        t = resp.get("task") if isinstance(resp.get("task"), dict) else {}
        return {k: _s(t.get(k), 200) for k in ("id", "subject") if t.get(k)}
    if tool == "TaskUpdate" and isinstance(resp, dict):
        out = {k: _s(resp.get(k), 60) for k in ("taskId",) if resp.get(k)}
        sc = resp.get("statusChange")
        if isinstance(sc, dict):
            out["from"] = _s(sc.get("from"), 30)
            out["to"] = _s(sc.get("to"), 30)
        if resp.get("success") is False:
            out["success"] = False
        return out
    if tool in ("Agent", "Task") and isinstance(resp, dict):
        out = {k: _s(resp.get(k), 80) for k in ("status", "agentId") if resp.get(k)}
        if resp.get("isAsync"):
            out["async"] = True
        return out
    if tool == "Read" and isinstance(resp, dict):
        out = {"type": _s(resp.get("type"), 30)} if isinstance(resp.get("type"), str) else {}
        f = resp.get("file") if isinstance(resp.get("file"), dict) else {}
        if f.get("filePath"):
            out["file_path"] = _s(f["filePath"], 500)
        for k, n in (("numLines", "lines"), ("startLine", "start_line"), ("totalLines", "total_lines"),
                     ("originalSize", "bytes")):
            if _num(f.get(k)) is not None:
                out[n] = f[k]
        if isinstance(f.get("content"), str):
            out["content_chars"] = len(f["content"])
        if isinstance(f.get("cells"), list):
            out["cells"] = len(f["cells"])
        return out
    if tool in ("Grep", "Glob", "LS") and isinstance(resp, dict):
        out = {"mode": _s(resp["mode"], 30)} if isinstance(resp.get("mode"), str) else {}
        for k, n in (("numFiles", "files"), ("numLines", "lines"), ("numMatches", "matches"),
                     ("totalFiles", "total_files"), ("truncated", "truncated")):
            v = resp.get(k)
            if _num(v) is not None or isinstance(v, bool):
                out[n] = v
        names = resp.get("filenames")
        if isinstance(names, list):
            out["paths"] = [_s(x, 300) for x in names[:10] if isinstance(x, str)]
            if len(names) > 10:
                out["paths_cut"] = len(names) - 10
        if isinstance(resp.get("content"), str):
            out["content_chars"] = len(resp["content"])
        return out
    if tool == "WebFetch" and isinstance(resp, dict):
        out = {n: resp[k] for k, n in (("code", "code"), ("bytes", "bytes"), ("durationMs", "duration_ms"))
               if _num(resp.get(k)) is not None}
        if isinstance(resp.get("codeText"), str):
            out["code_text"] = _s(resp["codeText"], 60)
        if resp.get("url"):
            out["url"] = _url(resp["url"])
        if isinstance(resp.get("result"), str):
            out["result_chars"] = len(resp["result"])
        return out
    if tool == "WebSearch" and isinstance(resp, dict):
        out = {"query": _s(resp.get("query"), 200)} if resp.get("query") else {}
        if isinstance(resp.get("results"), list):
            out["results"] = len(resp["results"])
        return out
    # Anything else (MCP tools, new built-ins): which keys it had and how big
    # it was, never its values (a tool's result can hold anything).
    try:
        size = len(json.dumps(resp, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        size = len(str(resp))
    if isinstance(resp, dict):
        return {"keys": sorted(str(k) for k in resp.keys())[:20], "chars": size}
    if isinstance(resp, list):
        return {"items": len(resp), "chars": size}
    return {"chars": size} if resp not in (None, "") else {}
