"""Scrubbing before anything reaches the journal (PLAN-v4 phase 2).

Everything checkpoint journals passes through here first: prompt text,
commands, tool inputs and outputs, notes, subagent messages. The journal is a
record for recovery, not a log of values, so this errs on the side of
masking: a masked count or path costs little, a stored token costs a lot.

- `scrub(text)`: token shapes (GitHub ghp_/gho_/ghu_/ghs_/ghr_/github_pat_,
  GitLab glpat-, Hugging Face hf_, OpenAI/Anthropic sk-..., Stripe sk_/rk_,
  Slack xox?-, AWS AKIA/ASIA key ids and secret keys, Google AIza..., npm_,
  pypi-, age secret keys, JWTs), PEM private key blocks, URLs with
  user:pass@ (an empty user too: `redis://:pw@`), Authorization/Bearer
  headers, Telegram bot tokens, `--password x`-style flags, the
  command-line credentials only their program names (mysql/mariadb and
  mongo/mongosh `-p<pw>` and `-p <pw>`, curl `-u user:<pw>` (bundled too:
  `-su`, `-fsSLu`) and `-E/--cert file:<pw>`, `sshpass -p`,
  docker/podman/helm/nerdctl/az `login -p` (login as the subcommand, also
  after global options), `redis-cli -a`, `htpasswd -b`, openssl
  `-k`/`-K`/`-pass pass:`, the ldap tools' `-w`, smbclient's `-U
  user%<pw>` and its password after the share, what `echo` pipes into
  `sudo -S`; after any separator (a backslash-newline is a space), inside
  a command line quoted in another (`ssh host '...'`, `ssh host "bash -c
  \\"...\\""`, `sh -c "..."`, `bash -o pipefail -c '...'`, `docker exec db sh
  -c '...'`, `su -c`, `script -qc`, `eval`, `tmux`), and in what a command
  substitution runs (`$(...)`, backticks, also inside double quotes); a `#`
  comment, a here-document's body and a contraction's apostrophe (`can't`)
  open no quote that hides a later line, and each line is read again on its
  own), and
  `*KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*` assignments in
  shell, env, YAML and JSON text (names with separators, camelCase, or
  none: PGPASSWORD, APITOKEN; a value up to the next space, commas
  included), each replaced with `***` (the name is kept, the value goes).
- `scrub_value(obj)`: the same inside a JSON-like value, and any dict field
  whose name says key/token/secret/password/credential/auth/cookie loses its
  value whatever it looks like.
- `tool_input(tool, input)` / `tool_response(tool, response)`: bounded,
  scrubbed summaries. Known tools keep what recovery needs (the command, the
  file path, the plan step); file contents and edit strings are never kept
  (their size and hash are), neither are Read, Grep, WebFetch or WebSearch
  results (paths, counts and sizes are), nor the output of a command that
  isn't a test, build or check (its size is; a failed one keeps Claude
  Code's first line, the exit code or an interrupt: `failure_mark`); an
  unknown tool (an MCP tool, say) keeps a short scrubbed summary of its
  input and only the keys and size of its result.
- `clip(text, n)`: text cut to n characters with a marker.

Patterns, not a vault: checkpoint holds no secrets of its own to compare
against (the gateway owns those and scrubs its own output first), so a
secret in a form no pattern knows (a bare random string, a tool's own
flag) stays: what leaves the machine (an export) says to review it first.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
from typing import Any, Iterator

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
_WORDS = ("key", "keys", "token", "secret", "secrets", "password", "passwords", "passwd", "pwd", "pass", "sshpass",
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
# Tried only where a run of name characters starts (its leading digits,
# dots and dashes are no name: `9TOKEN=x` is TOKEN's), so a long word costs
# its length, not its square: every later start in the same run ends where
# the first one does.
_ASSIGN = re.compile(r"(?<![A-Za-z0-9_.-])[0-9.-]*+(?P<name>[A-Za-z_][A-Za-z0-9_.-]*+)(?P<sep>\s*[=:]\s*)"
                     r"(?P<val>\"(?:\\.|[^\"\\])*\"|'[^']*'|[^\s;&|)}\]]+)")
# "name": "value" in JSON text (escaped quotes inside the value handled).
_JSON_FIELD = re.compile(r"(?P<q>\\?\")(?P<name>[^\"\\]{1,80})(?P=q)(?P<sep>\s*:\s*)"
                         r"(?P<val>(?P=q)(?:\\.|[^\"\\])*?(?P=q)|-?\d[\d.]*|true|false)")
# --password VALUE / --token=VALUE (a sensitive name). A short flag like -p
# says nothing by itself: _cli_secrets reads it only after its program.
# Tried once per run of flag characters (`lead`: what comes before the
# flag in that run), and only when that run ends in `=` or a space: a long
# word with many dashes costs its length, not its square.
_FLAG = re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*+[=\s])(?P<lead>[A-Za-z0-9_-]*?)"
                   r"(?P<flag>--?(?P<name>[A-Za-z][A-Za-z0-9_-]*+))(?P<sep>=|\s+)(?P<val>\"[^\"]*\"|'[^']*'|[^\s-][^\s]*)")
# scheme://user:pw@ (an empty user too: redis://:pw@host).
_URL_CRED = re.compile(r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.-]{0,63}://)(?P<user>[^/\s:@'\"]*):(?P<pw>[^/\s@'\"]+)@")
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
# A Telegram bot token: in its API URL (`/bot<id>:<token>/`) or bare.
_BOT_TOKEN = re.compile(r"(?P<pre>\bbot\d{5,12}:)[A-Za-z0-9_-]{30,}|\b\d{8,12}:AA[A-Za-z0-9_-]{33}(?![A-Za-z0-9_-])")

# Command-line credentials whose flag name says nothing (`-p`, `-a`, `-u`,
# `-k`, `-w`): only after the program that reads them that way, so `ssh -p
# 2222`, `mkdir -p`, `cp -p`, `git log -p` and `tar -czpf` stay as they are.
_CLI_HINT = re.compile(r"mysql|mariadb|sshpass|curl|wget|login|redis-cli|htpasswd|openssl|sudo|-stdin|chpasswd"
                       r"|mongo|ldap|smb|rpcclient|samba-tool|\bnet\b")
# The command line is cut into words and separators by _cli_tokens.
_MYSQL = re.compile(r"mysql\w*|mariadb[\w-]*")
_MONGO = re.compile(r"mongo(?:sh|dump|restore|export|import|stat|top|files)?")
_LOGIN_TOOLS = ("docker", "podman", "helm", "nerdctl", "buildah", "skopeo", "oras", "az")
# Their global options that take a value (before the subcommand: `docker
# --config d login`), and the words before `login` in a two-word subcommand.
_LOGIN_GLOBAL_ARGS = ("--config", "-c", "--context", "-H", "--host", "-l", "--log-level", "--tlscacert", "--tlscert",
                      "--tlskey", "--connection", "--identity", "--url", "--root", "--runroot", "--storage-driver",
                      "--kubeconfig", "--kube-context", "-n", "--namespace", "--registry-config", "--subscription",
                      "-o", "--output", "--query", "--address", "--storage-opt")
_LOGIN_GROUPS = ("registry", "acr")
_SUDO_ARG_OPTS = ("-u", "-g", "-p", "-C", "-h", "-r", "-t", "-U", "-T", "-D")
# curl's short options that take a value (the rest are flags, and a bundle
# like -fsSLu ends with the one that takes it: `-su user:pw`, `-sualice:pw`).
_CURL_ARGS = "AbcCdDeEFHKmoPQrtTuUwxXyYz"
# The ldap tools' short options that take a value (ldappasswd's: -a old, -s
# new and -w the bind password are secrets; the others' -w).
_LDAP_ARGS = "abdDeEfFhHloOpPRsSTUwXyYz"
_LDAPPASSWD_ARGS = "aDeEhHoOpRstTUwXyY"
_SMB_TOOLS = ("smbclient", "rpcclient", "smbcacls", "smbcquotas", "smbtree", "smbget", "net", "samba-tool")
# Programs that run a quoted argument as a command line of their own: a shell
# with -c, su/runuser/flock/script with -c, eval, watch, and ssh's remote
# command. The quoted string is read again as a command line (when it holds
# a hint): `ssh host 'mysql -p<pw>'`, `docker exec db sh -c "..."`.
_SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "yash", "fish", "busybox")
_SH_LONG_ARGS = ("--rcfile", "--init-file")     # a shell's long options that take a value
_DASH_C = ("su", "runuser", "flock", "script")
_SSH_ARGS = "bcDEeFIiJLlmOoPpQRSWwB"
_INNER_DEPTH = 8        # command lines quoted or substituted in one another, read again this deep
# The words after a program that its options are looked for in: a credential
# comes within its command's first words, and a text naming a program
# thousands of times in one line (prose) costs its length, not its square.
_ARGS_MAX = 256

_Words = list[tuple[int, int, str]]


def _masked(word: str, keep: int = 0) -> str:
    """A word with its value (after `keep` characters) masked; quotes kept."""
    head, val = word[:keep], word[keep:]
    if val[:1] in ("'", '"') and len(val) >= 2:
        return f"{head}{val[0]}{MASK}{val[0]}"
    return head + MASK


def _opt_values(words: _Words, letters: str, stop: bool = False) -> _Words:
    """Edits masking the values of the short options `letters` (`-p VALUE`,
    `-pVALUE`); with `stop`, only the options before the first operand
    (sshpass: the command it runs has options of its own)."""
    out = []
    i = 0
    while i < len(words):
        s, e, w = words[i]
        if w == "--" or stop and not w.startswith("-"):
            break
        if len(w) == 2 and w[0] == "-" and w[1] in letters:
            if i + 1 < len(words) and not words[i + 1][2].startswith("-"):
                out.append((words[i + 1][0], words[i + 1][1], _masked(words[i + 1][2])))
                i += 1
        elif len(w) > 2 and w[0] == "-" and w[1] in letters:
            out.append((s, e, _masked(w, 2)))
        i += 1
    return out


def _bundled(words: _Words, takes: str, secret: str) -> list[tuple[int, int, str, int, str]]:
    """The values of short options in bundles (`-xw VALUE`, `-xwVALUE`,
    curl's `-fsSLu user:pw`): each word `-abc...` is read letter by letter;
    the first letter that takes a value (`takes`) gets the rest of the word,
    or the next word when nothing is left. For each such letter in `secret`:
    (start, end, word, offset of the value in it, the letter)."""
    out = []
    i = 0
    while i < len(words):
        s, e, w = words[i]
        if w == "--":
            break
        if len(w) > 1 and w[0] == "-" and w[1] != "-":
            for j in range(1, len(w)):
                c = w[j]
                if c not in takes:
                    continue
                if j + 1 < len(w):
                    if c in secret:
                        out.append((s, e, w, j + 1, c))
                elif i + 1 < len(words):
                    if c in secret:
                        out.append((*words[i + 1], 0, c))
                    i += 1
                break
        i += 1
    return out


def _after(word: str, at: int, mark: str, s: int, e: int) -> _Words:
    """The edit masking what follows the first `mark` (`:` of user:pw, `%`
    of smbclient's user%pw) in the value starting at `at` (an empty user
    counts: `:token`); none when nothing follows it."""
    q = word[at:at + 1] if word[at:at + 1] in ("'", '"') else ""
    cut = word.find(mark, at + len(q))
    if cut < 0 or cut >= len(word) - 1 - len(q):
        return []
    return [(s, e, word[:cut + 1] + MASK + q)]


def _cert(word: str, at: int, s: int, e: int) -> _Words:
    """curl's `-E/--cert <file>:<password>`: the part after the first colon
    (a `pkcs11:` URI names a key where it is and stays)."""
    q = 1 if word[at:at + 1] in ("'", '"') else 0
    if word[at + q:].startswith("pkcs11:"):
        return []
    return _after(word, at, ":", s, e)


def _user_pw(words: _Words, prog: str) -> _Words:
    """curl `-u user:pw` (bundled too: `-su`, `-fsSLu`), `--user user:pw`,
    `--user=user:pw` and the proxy's -U, `--proxy-user`; wget: the long
    forms only (its -U is the user agent): the part after the first colon.
    curl's `-E/--cert file:pw` (and `--proxy-cert`): the password."""
    out: _Words = []
    for i, (s, e, w) in enumerate(words):
        if w in ("--user", "--proxy-user") and i + 1 < len(words):
            out += _after(words[i + 1][2], 0, ":", words[i + 1][0], words[i + 1][1])
        elif w.startswith(("--user=", "--proxy-user=")):
            out += _after(w, w.index("=") + 1, ":", s, e)
        elif prog == "curl" and w in ("--cert", "--proxy-cert") and i + 1 < len(words):
            out += _cert(words[i + 1][2], 0, words[i + 1][0], words[i + 1][1])
        elif prog == "curl" and w.startswith(("--cert=", "--proxy-cert=")):
            out += _cert(w, w.index("=") + 1, s, e)
    if prog == "curl":
        for s, e, w, at, c in _bundled(words, _CURL_ARGS, "uUE"):
            out += _cert(w, at, s, e) if c == "E" else _after(w, at, ":", s, e)
    return out


def _smb_user(words: _Words, prog: str) -> _Words:
    """smbclient (rpcclient, net, ...) `-U user%pw`, `-Uuser%pw`,
    `--user=user%pw`: the part after the `%`; smbclient's password as the
    operand after the share (`smbclient //server/share PASSWORD`)."""
    out: _Words = []
    for i, (s, e, w) in enumerate(words):
        if w in ("-U", "--user") and i + 1 < len(words):
            out += _after(words[i + 1][2], 0, "%", words[i + 1][0], words[i + 1][1])
        elif w.startswith("--user="):
            out += _after(w, len("--user="), "%", s, e)
        elif w.startswith("-U") and len(w) > 2:
            out += _after(w, 2, "%", s, e)
    if prog == "smbclient":
        at = next((i for i, x in enumerate(words) if x[2].lstrip("'\"").startswith(("//", "\\\\"))), -1)
        if 0 <= at < len(words) - 1 and not re.match(r"-|\d*[<>&]", words[at + 1][2]):     # (not a redirection)
            out.append((words[at + 1][0], words[at + 1][1], _masked(words[at + 1][2])))
    return out


def _login_sub(words: _Words) -> bool:
    """Is `login` the subcommand (`docker login`, `docker --config d login`,
    `podman --cgroup-manager systemd login`, `helm registry login`, `az acr
    login`), not a word further on (`docker run -p 8080:80 img login`)? A
    global option's value is skipped: the known ones' always, any other long
    option's when `login` or another option comes right after it."""
    first = ""
    i = 0
    n = len(words)
    while i < n:
        w = words[i][2]
        if w.startswith("-"):
            if w in _LOGIN_GLOBAL_ARGS:
                i += 2
            elif (w.startswith("--") and "=" not in w and i + 2 < n and words[i + 1][2] != "login"
                  and not words[i + 1][2].startswith("-")
                  and (words[i + 2][2] == "login" or words[i + 2][2].startswith("-"))):
                i += 2      # an unknown global option and its value (`--cgroup-manager systemd login`)
            else:
                i += 1
            continue
        if first in _LOGIN_GROUPS:
            return w == "login"
        if first:
            return False
        if w == "login":
            return True
        first = w
        i += 1
    return False


def _htpasswd(words: _Words) -> _Words:
    """`htpasswd -b [file] user password` (-n: no file): the password."""
    flags = "".join(w[1:] for _s, _e, w in words if w.startswith("-") and not w.startswith("--"))
    if "b" not in flags:
        return []
    operands = [x for x in words if not x[2].startswith("-")]
    at = 1 if "n" in flags else 2
    return [(operands[at][0], operands[at][1], _masked(operands[at][2]))] if len(operands) > at else []


def _openssl(words: _Words) -> _Words:
    """openssl `-k PASSWORD`, `-K HEXKEY`, `-pass/-passin/-passout pass:PASSWORD`
    (env:, file: and fd: name where it is, and stay)."""
    out = []
    for i, (_s, _e, w) in enumerate(words[:-1]):
        s, e, v = words[i + 1]
        if w in ("-k", "-K"):
            out.append((s, e, _masked(v)))
        elif w in ("-pass", "-passin", "-passout", "-password"):
            q = v[:1] if v[:1] in ("'", '"') else ""
            if v[len(q):].startswith("pass:"):
                out.append((s, e, f"{q}pass:{MASK}{q}"))
    return out


def _stdin_target(words: _Words) -> bool:
    """Does this command read a password on stdin (`sudo -S`, `--password-stdin`,
    `chpasswd`, `passwd --stdin`)?"""
    ws = [w for _s, _e, w in words]
    for i, w in enumerate(ws):
        prog = w.rsplit("/", 1)[-1]
        lim = min(len(ws), i + 1 + _ARGS_MAX)
        if prog == "sudo":
            j = i + 1     # sudo's own options, before the command it runs
            while j < lim and ws[j].startswith("-") and ws[j] != "--":
                if ws[j] == "--stdin" or re.fullmatch(r"-[A-Za-z]*S[A-Za-z]*", ws[j]):
                    return True
                j += 2 if ws[j] in _SUDO_ARG_OPTS else 1
        if prog == "chpasswd" or prog == "passwd" and "--stdin" in ws[i + 1:]:
            return True
    return "--password-stdin" in ws


# An apostrophe in a prose contraction (can't, it's, we're, I'll, I've,
# I'm, I'd): after a letter, one of these, then a space, punctuation or the
# end. It opens no quote and closes none (should-fix 4 of round 3): `I can't
# log in with mysql -p<pw>, it's denied` is words, and `Ask O'Neil: mysql
# -p<pw>, it can't connect` hides nothing. Not before `/`, `'` or a letter:
# `sed -e's/a/b/'` and `-e't'` stay quotes.
_CONTRACTION = re.compile(r"(?:s|t|re|ll|ve|m|d)(?=[\s.,;:!?)]|\Z)")
# A here-document's operator (`<<WORD`, `<<-WORD`; never the here-string `<<<`).
_HEREDOC_OP = re.compile(r"(?<!<)<<(?!<)(-?)")


def _apostrophe(text: str, j: int) -> bool:
    """Is the `'` at text[j] a contraction's apostrophe (no quote)?"""
    if not (j > 0 and text[j - 1].isalpha() and _CONTRACTION.match(text, j + 1) is not None):
        return False
    k = j - 1
    while k > 0 and text[k - 1].isalpha():
        k -= 1
    # an option's quoted value (`-e's,a,b,'`, `-F't '`) is a quote; `co-worker's` is not an option
    return not (k > 0 and text[k - 1] == "-" and (k == 1 or text[k - 2] in " \t\n-"))


def _heredoc_delim(text: str, p: int, e: int) -> tuple[str, bool, int] | None:
    """A here-document's delimiter word at text[p:e] (`EOF`, `'EOF'`,
    `"EOF"`, `\\EOF`): (the delimiter without its quotes, whether any part
    was quoted, where the word ends); None when there is none."""
    out: list[str] = []
    quoted = False
    while p < e:
        c = text[p]
        if c in "'\"":
            k = text.find(c, p + 1, e)
            if k < 0:
                return None
            out.append(text[p + 1:k])
            quoted, p = True, k + 1
        elif c == "\\" and p + 1 < e:
            out.append(text[p + 1])
            quoted, p = True, p + 2
        elif c in " \t\r\n;&|()<>":
            break
        else:
            out.append(c)
            p += 1
    d = "".join(out)
    return (d, quoted, p) if d else None


def _heredoc_end(text: str, p: int, b: int, delim: str, dash: bool,
                 seen: dict[tuple[str, bool], tuple[int, tuple[int, int] | None]]) -> tuple[int, int] | None:
    """Where the body starting at text[p] (just after a newline) ends: (the
    start of its delimiter line, just after that line); None when no line
    before b is the delimiter (`<<-`: after leading tabs). `seen` remembers
    each delimiter's last answer, so a text of many unterminated operators
    costs its length, not its square."""
    hit = seen.get((delim, dash))
    if hit is not None and hit[0] <= p and (hit[1] is None or hit[1][0] >= p):
        return hit[1]
    m = re.compile("^" + ("\t*" if dash else "") + re.escape(delim) + "$", re.M).search(text, p, b)
    res = (m.start(), min(m.end() + 1, b)) if m else None
    seen[(delim, dash)] = (p, res)
    return res


def _word_end(text: str, i: int, b: int, subs: list[tuple[int, int]] | None = None,
              parts: list[tuple[int, int]] | None = None) -> int:
    """The end of the shell word that starts at text[i] (before b): quoted
    strings, `$(...)` and backticks are kept whole and balanced (a `$(...)`
    holds quotes of its own, also inside double quotes); a backslash-newline
    ends it like a space; so does a quote of its own that is never closed
    (an apostrophe in prose: `O'Neil`; skipped like a space), never taking
    the rest of the text with it. A contraction's apostrophe (`can't`) is
    no quote; inside a substitution a here-document's body is skipped (the
    commit form `"$(cat <<'EOF' ... EOF\\n)"`: an apostrophe in the message
    pairs with nothing).
    Into `subs`: (start, end) of what each command substitution at the
    word's own level runs (`$(...)` and backticks, also inside double
    quotes; never inside single quotes, and one that isn't closed runs to
    the end); into `parts`: (start, end) of each quoted string at its level
    ('...', "...", $'...')."""
    end, sb, pt, stop = _scan_word(text, i, b)
    if stop >= 0:       # a quote of its own never closed: the word ends before it
        end, sb, pt, _stop = _scan_word(text, i, stop) if stop > i else (i, [], [], -1)
    if subs is not None:
        subs.extend(sb)
    if parts is not None:
        parts.extend(pt)
    return end


def _scan_word(text: str, i: int, b: int, body: bool = False
               ) -> tuple[int, list[tuple[int, int]], list[tuple[int, int]], int]:
    """_word_end's scan: (end, substitutions, quoted parts, where a quote of
    the word's own level that is never closed starts, or -1). With `body`,
    text[i:b] is an unquoted here-document's body: read like the inside of
    double quotes that never close (quotes are characters, `$(...)` and
    backticks run)."""
    subs: list[tuple[int, int]] = []
    parts: list[tuple[int, int]] = []
    # [kind, where its text starts (a substitution at the word's level; a quote's own start) or -1];
    # "H": an unquoted here-document's body.
    stack: list[list[Any]] = [["H", -1]] if body else []
    heredocs: list[tuple[str, bool, bool]] = []     # inside a substitution: bodies to skip at its next newline
    seen: dict[tuple[str, bool], tuple[int, tuple[int, int] | None]] = {}
    j = i
    while j < b:
        c = text[j]
        kind = stack[-1][0] if stack else ""
        if c == "\\":
            if not stack and text.startswith("\n", j + 1):
                break
            j += 2
            continue
        if kind == "`":
            if c == "`":
                k, st = stack.pop()
                if st >= 0:
                    subs.append((st, j))
            j += 1
            continue
        level = all(k in ('"', "H") for k, _st in stack)
        if kind in ('"', "H"):
            if c == '"' and kind == '"':
                k, st = stack.pop()
                if st >= 0:
                    parts.append((st, j + 1))
                j += 1
            elif c == "$" and text.startswith("(", j + 1):
                stack.append(["(", j + 2 if level else -1])
                j += 2
            else:
                if c == "`":
                    stack.append(["`", j + 1 if level else -1])
                j += 1
            continue
        if c == "'" and _apostrophe(text, j):
            j += 1      # can't, it's: no quote
            continue
        if c == "'" or (c == "$" and text.startswith("'", j + 1)):
            k = text.find("'", j + (2 if c == "$" else 1), b)
            while k >= 0 and _apostrophe(text, k):      # nor does one close it (O'Neil ... can't)
                k = text.find("'", k + 1, b)
            if k >= 0:
                if not stack:
                    parts.append((j, k + 1))
                j = k + 1
                continue
            if not stack:
                return j, subs, parts, j    # never closed, at the word's level: the word ends here
            # never closed inside a substitution: an ordinary character (below)
        elif c == '"':
            stack.append(['"', j if not stack else -1])
            j += 1
            continue
        elif c == "`":
            stack.append(["`", j + 1 if level else -1])
            j += 1
            continue
        elif c == "$" and text.startswith("(", j + 1):
            stack.append(["(", j + 2 if level else -1])
            j += 2
            continue
        if kind == "(":
            if c == "(":
                stack.append(["(", -1])
            elif c == ")":
                k, st = stack.pop()
                if st >= 0:
                    subs.append((st, j))
            elif c == "<" and (m := _HEREDOC_OP.match(text, j)):
                p = m.end()
                while p < b and text[p] in " \t":
                    p += 1
                d = _heredoc_delim(text, p, b)
                if d:
                    heredocs.append((d[0], bool(m.group(1)), d[1]))
                    j = d[2]
                    continue
            elif c == "\n" and heredocs:
                # The bodies of this line's here-documents: text, not commands
                # (their own substitutions are read when this one is read again).
                p = j + 1
                for delim, dash, _quoted in heredocs:
                    end = _heredoc_end(text, p, b, delim, dash, seen)
                    if end is None:
                        break   # never terminated: read on as before
                    p = end[1]
                heredocs = []
                j = p
                continue
            j += 1
            continue
        # The word's own level, outside quotes.
        if c in "<>" and text.startswith("&", j + 1) or c == "&" and text.startswith(">", j + 1):
            j += 2      # 2>&1, &>file: part of the word
            continue
        if c in " \t\r\f\v\n|&;()":
            break
        j += 1
    j = min(j, b)
    if stack and stack[0][0] == '"':
        return j, subs, parts, stack[0][1]     # its double quote never closed: the word ends before it
    subs.extend((st, j) for k, st in stack if st >= 0 and k != '"')     # not closed: runs to the end
    return j, subs, parts, -1


def _cli_tokens(text: str, a: int, b: int) -> Iterator[tuple[str, int, int, list[tuple[int, int]], list[tuple[int, int]]]]:
    """text[a:b] as a command line: ("word", start, end, substitutions,
    quoted parts) (see _word_end) and ("sep", start, end, [], []) for
    `&&`, `||`, `;`, `|`, `&`, `(`, `)` and a newline outside quotes;
    `2>&1` and `&>` are words, a backslash-newline is a space. As the shell
    reads them: a `#` comment ends at its newline (its words are still
    yielded, so a commented-out `# mysql -p<pw>` loses it too, but its
    quotes end there: `# Don't ...`), and a here-document's body (`<<WORD`,
    `<<-WORD`, `<<'WORD'`, `<<"WORD"`, up to the line that is WORD) is no
    command: skipped, an unquoted one's substitutions yielded as
    ("body", start, end, substitutions, []). One never terminated is read
    on as before."""
    j = a
    lim, comment = b, False     # in a comment: where its line ends
    heredocs: list[tuple[str, bool, bool]] = []     # this line's: (delimiter, <<-, quoted)
    delim_next: bool | None = None      # after a bare `<<` / `<<-`: the next word is its delimiter
    seen: dict[tuple[str, bool], tuple[int, tuple[int, int] | None]] = {}
    while j < b:
        if comment and j >= lim:
            lim, comment = b, False
        c = text[j]
        if c in " \t\r\f\v":
            j += 1
        elif c == "\\" and text.startswith("\n", j + 1) and j + 1 < lim:
            j += 2      # a line continuation: a space (`curl -u \<newline> user:pw`)
        elif c in "\n;()" or c in "|&" and not text.startswith("&>", j):
            n = 2 if text.startswith(("&&", "||"), j) else 1
            yield "sep", j, j + n, [], []
            j += n
            delim_next = None
            if c == "\n" and heredocs:
                for delim, dash, quoted in heredocs:
                    end = _heredoc_end(text, j, b, delim, dash, seen)
                    if end is None:
                        break   # never terminated: read on as before
                    if not quoted:      # `$(...)` and backticks in it run
                        sb = _scan_word(text, j, end[0], body=True)[1]
                        if sb:
                            yield "body", j, end[0], sb, []
                    j = end[1]
                heredocs = []
        elif c == "#" and not comment:
            lim, comment = text.find("\n", j, b), True
            lim = b if lim < 0 else lim
            j += 1
        else:
            subs: list[tuple[int, int]] = []
            parts: list[tuple[int, int]] = []
            e = _word_end(text, j, lim, subs, parts)
            if e <= j:
                j += 1      # a quote never closed: skipped like a space
                continue
            if not comment:
                _heredoc_ops(text, j, e, subs, parts, heredocs, delim_next)
                delim_next = None
                if text.endswith(("<<", "<<-"), j, e) and not text.endswith("<<<", j, e):
                    delim_next = text.endswith("-", j, e)
            yield "word", j, e, subs, parts
            j = e


def _heredoc_ops(text: str, s: int, e: int, subs: list[tuple[int, int]], parts: list[tuple[int, int]],
                 heredocs: list[tuple[str, bool, bool]], delim_next: bool | None) -> None:
    """The here-documents a word text[s:e] opens, added to `heredocs`: its
    `<<WORD` operators outside its quotes and substitutions, or the whole
    word when it is the delimiter of a bare `<<` before it (`delim_next`:
    whether that one was `<<-`)."""
    if delim_next is not None:
        d = _heredoc_delim(text, s, e)
        if d:
            heredocs.append((d[0], delim_next, d[1]))
        return
    for m in _HEREDOC_OP.finditer(text, s, e):
        k = m.start()
        if any(ps <= k < pe for ps, pe in parts) or any(ss <= k < se for ss, se in subs):
            continue
        d = _heredoc_delim(text, m.end(), e)
        if d:
            heredocs.append((d[0], bool(m.group(1)), d[1]))


def _quoted_span(text: str, s: int, e: int, parts: list[tuple[int, int]]) -> tuple[int, int, bool] | None:
    """When text[s:e] is one quoted string as a whole ('...', "...",
    $'...'): (start, end) of what is inside, and whether it was double
    quoted; else None."""
    if (s, e) not in parts or e - s < 2:
        return None
    if text[s] == "$":
        return (s + 2, e - 1, False) if e - s >= 3 else None
    return s + 1, e - 1, text[s] == '"'


def _inner_words(text: str, words: _Words, parts: dict[int, list[tuple[int, int]]]) -> list[tuple[int, int, bool]]:
    """What a segment runs as a command line of its own, quoted in one of
    its words: (start, end, double-quoted) of the text inside the quotes.
    A shell's `-c` string (`sh -c`, `bash -lc`, `bash -o pipefail -c`,
    `bash -euo pipefail -c`, `sh -c -- '...'`, `docker exec db sh -c`),
    `su ... -c` / `--command=` (runuser, flock, `script -qc`), `vagrant ssh
    -c`, `eval '...'`, `watch '...'`, tmux's (`tmux new-session -d '...'`),
    and ssh's remote command (every quoted word after the host)."""
    n = len(words)
    picked: set[int] = set()
    tail = n        # every word from here on (eval, watch, ssh's remote command)
    extra: list[tuple[int, int, bool]] = []
    for i in range(n):
        w = words[i][2]
        prog = w.rsplit("/", 1)[-1]
        lim = min(n, i + 1 + _ARGS_MAX)     # its options: within its first words (`su su su ...` costs n, not n²)
        if prog in _SHELLS:
            j, dash_c = i + 1, False
            while j < lim:
                o = words[j][2]
                if o == "--":
                    j += 1
                    break
                if len(o) < 2 or o[0] not in "-+":
                    break
                if o.startswith("--"):
                    j += 2 if o in _SH_LONG_ARGS else 1
                    continue
                dash_c = dash_c or "c" in o[1:]
                j += 1 + o.count("o") + o.count("O")     # -o NAME / -O NAME take the next word (-euo pipefail)
            if dash_c and j < n:
                picked.add(j)       # the first operand after the options: the command string
        elif prog in _DASH_C:
            for j in range(i + 1, lim):
                o = words[j][2]
                if o[:1] != "-":
                    continue
                if (o == "--command" or re.fullmatch(r"-[A-Za-z]*c", o)) and j + 1 < n:
                    picked.add(j + 1)
                elif o.startswith("--command="):
                    sp = _quoted_span(text, words[j][0] + 10, words[j][1], parts.get(words[j][0], []))
                    if sp:
                        extra.append(sp)
        elif prog == "vagrant" and i + 1 < n and words[i + 1][2] == "ssh":
            picked.update(j + 1 for j in range(i + 2, min(n - 1, lim)) if words[j][2] in ("-c", "--command"))
        elif prog in ("eval", "watch", "tmux"):
            tail = min(tail, i + 1)
        elif prog == "ssh":
            j = i + 1
            while j < lim and words[j][2].startswith("-"):
                o = words[j][2]
                j += 2 if len(o) == 2 and o[1] in _SSH_ARGS else 1
            tail = min(tail, j + 1)     # after the host: the remote command
    out = list(extra)
    for j in sorted(picked | set(range(tail, n))):
        sp = _quoted_span(text, words[j][0], words[j][1], parts.get(words[j][0], []))
        if sp:
            out.append(sp)
    return out


def _unescaped(text: str, s: int, e: int) -> tuple[str, list[int]]:
    """The inside of a double-quoted string as the command it quotes sees it
    (\\" \\\\ \\$ \\` lose their backslash, a backslash-newline goes), and
    where each of its characters is in text (one more: the end)."""
    out: list[str] = []
    pos: list[int] = []
    j = s
    while j < e:
        c = text[j]
        if c == "\\" and j + 1 < e and text[j + 1] in '"\\$`\n':
            if text[j + 1] != "\n":
                out.append(text[j + 1])
                pos.append(j)
            j += 2
            continue
        out.append(c)
        pos.append(j)
        j += 1
    pos.append(e)
    return "".join(out), pos


def _cli_edits_dq(text: str, s: int, e: int, depth: int) -> _Words:
    """_cli_edits of a command line quoted in double quotes (text[s:e] is
    inside them): read as the command sees it, so `ssh h "bash -c \\"mysql
    -p<pw>\\""` loses its password too; the edits are placed back in text
    (what a replacement keeps of its word stays as it was written there)."""
    if "\\" not in text[s:e]:
        return _cli_edits(text, s, e, depth)
    inner, pos = _unescaped(text, s, e)
    out: _Words = []
    for cs, ce, rep in _cli_edits(inner, 0, len(inner), depth):
        old = inner[cs:ce]
        p = 0
        while p < min(len(old), len(rep)) and old[p] == rep[p]:
            p += 1
        q = 0
        while q < min(len(old), len(rep)) - p and old[-1 - q] == rep[-1 - q]:
            q += 1
        mid = re.sub(r'(["\\$`])', r"\\\1", rep[p:len(rep) - q])
        out.append((pos[cs + p], pos[ce - q], mid))
    return out


_PROGS = {"sshpass", "curl", "wget", "redis-cli", "htpasswd", "openssl", *_LOGIN_TOOLS, *_SMB_TOOLS}


def _cli_edits(text: str, a: int, b: int, depth: int = 0) -> _Words:
    """The edits (start, end, replacement; positions in `text`) that mask
    the command-line credentials in text[a:b] (see _cli_secrets); a command
    line quoted in another, and what a command substitution runs (`$(...)`,
    backticks, also inside double quotes), are read again the same way."""
    segs: list[tuple[_Words, str]] = []     # each segment's words, and the separator after it
    words: _Words = []
    parts: dict[int, list[tuple[int, int]]] = {}
    subs: list[tuple[int, int]] = []
    for kind, s, e, sb, pt in _cli_tokens(text, a, b):
        if kind == "word":
            words.append((s, e, text[s:e]))
            if pt:
                parts[s] = pt
            subs += sb
        elif kind == "body":
            subs += sb      # what an unquoted here-document's body runs
        else:
            segs.append((words, text[s:e]))
            words = []
    segs.append((words, ""))
    edits: _Words = []
    for k, (words, _sep) in enumerate(segs):
        for i, (_s, _e, w) in enumerate(words):
            prog = w.rsplit("/", 1)[-1]
            if not (prog in _PROGS or _MYSQL.fullmatch(prog) or _MONGO.fullmatch(prog) or prog.startswith("ldap")):
                continue
            rest = words[i + 1:i + 1 + _ARGS_MAX]
            if _MYSQL.fullmatch(prog) or _MONGO.fullmatch(prog):
                edits += _opt_values(rest, "p")
            elif prog == "sshpass":
                edits += _opt_values(rest, "p", stop=True)
            elif prog in ("curl", "wget"):
                edits += _user_pw(rest, prog)
            elif prog in _LOGIN_TOOLS and _login_sub(rest):
                edits += _opt_values(rest, "p")
            elif prog == "redis-cli":
                edits += _opt_values(rest, "a")
            elif prog == "htpasswd":
                edits += _htpasswd(rest)
            elif prog == "openssl":
                edits += _openssl(rest)
            elif prog.startswith("ldap") and len(prog) > 4:
                pw = prog == "ldappasswd"
                edits += [(s, e, _masked(v, at)) for s, e, v, at, _c in
                          _bundled(rest, _LDAPPASSWD_ARGS if pw else _LDAP_ARGS, "asw" if pw else "w")]
            elif prog in _SMB_TOOLS:
                edits += _smb_user(rest, prog)
        if depth < _INNER_DEPTH:
            for s, e, dq in _inner_words(text, words, parts):
                if _CLI_HINT.search(text, s, e):
                    edits += _cli_edits_dq(text, s, e, depth + 1) if dq else _cli_edits(text, s, e, depth + 1)
        if not _stdin_target(words):
            continue
        for i, (s, e, w) in enumerate(words):     # `sudo -S cmd <<< pw`
            if w == "<<<" and i + 1 < len(words):
                edits.append((words[i + 1][0], words[i + 1][1], _masked(words[i + 1][2])))
            elif w.startswith("<<<") and len(w) > 3:
                edits.append((s, e, "<<<" + _masked(w[3:])))
        prev = segs[k - 1][0] if k and segs[k - 1][1] == "|" else []
        at = next((i for i in range(len(prev) - 1, -1, -1) if prev[i][2].rsplit("/", 1)[-1] in ("echo", "printf")), -1)
        if at >= 0:     # what the last echo/printf before the pipe prints
            args = [x for x in prev[at + 1:] if not re.fullmatch(r"-[neE]+", x[2])]
            if args:
                edits.append((args[0][0], args[-1][1], MASK))
    if depth < _INNER_DEPTH:
        for s, e in subs:       # what `$(...)` and backticks run: `n="$(mysql -p<pw> -e ...)"`
            if _CLI_HINT.search(text, s, e):
                edits += _cli_edits(text, s, e, depth + 1)
    return edits


def _cli_secrets(text: str) -> str:
    """The values of command-line credentials that only their program says
    are credentials: mysql/mariadb and mongo/mongosh `-p<pw>` / `-p <pw>`,
    curl/wget `-u user:<pw>` (curl's bundles too: `-su`, `-fsSLu`), `sshpass
    -p`, `docker|podman|helm|nerdctl|az ... login -p` (login as the
    subcommand), `redis-cli -a`, `htpasswd -b`, openssl `-k`/`-K`/`-pass
    pass:`, the ldap tools' `-w`, smbclient's `-U user%<pw>`, and what
    `echo`/`printf` pipe into `sudo -S` (or another stdin password reader,
    or `<<<` after it); in every command line, also one quoted inside
    another (`ssh host '...'`, `sh -c "..."`, `docker exec db sh -c '...'`).
    Then each line that names a program is read again on its own: a quote
    that pairs across lines in prose or in a form the scanner doesn't know
    (`O'Neil` on one line, a quote on a later one) hides no line between.
    That pass reads the text the first one masked (a masked value reads the
    same again), so the two never compete for a span."""
    if not _CLI_HINT.search(text):
        return text
    text = _edited(text, _cli_edits(text, 0, len(text)))
    if "\n" in text:
        edits: _Words = []
        a = 0
        while a < len(text):
            b = text.find("\n", a)
            b = len(text) if b < 0 else b
            if b > a and _CLI_HINT.search(text, a, b):
                edits += _cli_edits(text, a, b)
            a = b + 1
        text = _edited(text, edits)
        # Then each run of lines joined by a backslash-newline, as one line
        # (`curl -fsS \` / `  -u user:<pw>` in a here-document's body).
        edits = []
        a = 0
        while a < len(text):
            b, c, joined = a, a, False
            while True:
                b = text.find("\n", c)
                if b < 0:
                    b = len(text)
                    break
                k = b
                while k > c and text[k - 1] == "\\":
                    k -= 1
                if (b - k) % 2 == 0:
                    break
                c, joined = b + 1, True
            if joined and _CLI_HINT.search(text, a, b):
                edits += _cli_edits(text, a, b)
            a = b + 1
        text = _edited(text, edits)
    return text


def _edited(text: str, edits: _Words) -> str:
    """text with the edits made (one inside another's span is dropped)."""
    if not edits:
        return text
    out, pos = [], 0
    for s, e, rep in sorted(edits):
        if s < pos:
            continue    # inside an edit already made
        out.append(text[pos:s])
        out.append(rep)
        pos = e
    out.append(text[pos:])
    return "".join(out)


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
        # `$PWD:/src` is a variable's value followed by text, never NAME: value
        # (`${DB_PASS:-x}` has its brace before the name and stays masked).
        ref = m.start("name") > 0 and text[m.start("name") - 1] == "$"
        if ref or not sensitive_name(name):
            if _CHAINED.fullmatch(val) and (val[-1] in "=:" or _SEP_NEXT.match(text, m.end())):
                out.append(text[pos:m.start("val")])
                pos = m.start("val")
                continue
            if depth >= 6:
                rep = f"{name}{sep}{MASK}" if _ASSIGN.search(val) else text[m.start("name"):m.end()]
            else:
                rep = f"{name}{sep}{_assign_all(val, depth + 1)}"
        elif val.lower() in _SCHEMES:   # `Authorization: Basic ***` (the credential is masked already)
            rep = text[m.start("name"):m.end()]
        elif val.startswith(("\"", "'")):
            rep = f"{name}{sep}{val[0]}{MASK}{val[0]}"
        else:
            rep = f"{name}{sep}{MASK}"
        out.append(text[pos:m.start("name")])
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
    return f"{m.group('lead')}{m.group('flag')}{m.group('sep')}{MASK}"


# scrub()'s answers while `remembered()` holds (None otherwise): at most
# _MEMO_MAX texts and _MEMO_CHARS characters of texts and answers together
# (past either, new texts are scrubbed without being remembered).
_MEMO: dict[str, str] | None = None
_MEMO_MAX = 200_000
_MEMO_CHARS = 16_000_000
_memo_chars = 0


@contextlib.contextmanager
def remembered() -> Iterator[None]:
    """Within this, scrub() remembers its answer for each text: a recovery
    scrubs every command of a task's history several times (the summary, the
    operations, what succeeded, the summary again). scrub is a pure function
    of its text, so only the time changes; the memory (bounded: _MEMO_MAX,
    _MEMO_CHARS) goes when the outermost `remembered()` ends."""
    global _MEMO, _memo_chars
    outer = _MEMO is not None
    if not outer:
        _MEMO = {}
        _memo_chars = 0
    try:
        yield
    finally:
        if not outer:
            _MEMO = None
            _memo_chars = 0


def scrub(text: Any) -> str:
    """Text with every secret shape masked (see the module docstring)."""
    global _memo_chars
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    memo = _MEMO
    if memo is not None:
        hit = memo.get(text)
        if hit is None:
            hit = _scrub(text)
            size = len(text) + len(hit)
            if len(memo) < _MEMO_MAX and _memo_chars + size <= _MEMO_CHARS:
                memo[text] = hit
                _memo_chars += size
        return hit
    return _scrub(text)


def _scrub(text: str) -> str:
    for rx in _SHAPES:
        text = rx.sub(MASK, text)
    text = _URL_CRED.sub(lambda m: f"{m.group('pre')}{m.group('user')}:{MASK}@", text)
    text = _AUTH.sub(lambda m: m.group("h") + MASK, text)
    text = _BEARER.sub(lambda m: m.group("h") + MASK, text)
    text = _AWS_SECRET.sub(lambda m: m.group("pre") + MASK, text)
    text = _BOT_TOKEN.sub(lambda m: (m.group("pre") or "") + MASK, text)
    text = _WORD_VALUE.sub(lambda m: m.group("w") + m.group("sep") + MASK, text)
    text = _cli_secrets(text)
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
# What Claude Code puts first in a failed command's error before the output:
# the exit code, or an interrupt's or a timeout's words.
_FAIL_MARK = re.compile(r"^\s*(Exit code -?\d+|\[Request interrupted[^\]\n]{0,80}\]|Interrupted by user"
                        r"|Command timed out[^\n]{0,80})")


def failure_mark(err: Any) -> str:
    """Claude Code's first line of a failed command's error ("Exit code 1",
    "Interrupted by user"), never the output after it; '' when it has none."""
    m = _FAIL_MARK.match(str(err or ""))
    return m.group(1) if m else ""


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
