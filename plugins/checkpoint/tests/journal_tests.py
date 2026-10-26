# The journal (PLAN-v4 phase 2): format, durability, capture, the sanitizer,
# the recovery summary, checkpoints, migration and overhead. Run by
# tests/run_tests.py in its own namespace (exec), so it shares that suite's
# helpers and its hermetic environment: check(), hook(), ctx(), pycalls(),
# fake_claude(), TMP, the temp CHECKPOINT_HOME, CHECKPOINT_PYTHON's counting
# wrapper. Synthetic secrets are generated here and never printed.
# ruff: noqa: F821  (names come from run_tests.py)
import errno as _errno
import hashlib as _hashlib
import platform as _platform
import random as _random
import resource as _resource
import secrets as _secrets
import string as _string
import threading as _threading

from checkpoint import capture, journal, sanitize, summary  # noqa: E402

JT = TMP / "journal-tests"
JT.mkdir()
JGIT = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
JGIT.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=str(TMP.resolve()))
HENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _rand(n, alphabet=_string.ascii_letters + _string.digits):
    return "".join(_secrets.choice(alphabet) for _ in range(n))


def jgit(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True, timeout=30, env=JGIT)


def jrecs(task):
    return journal.read(task).records


def kinds(task):
    return [r["kind"] for r in jrecs(task)]


def task_of(sid):
    return state.load_session(sid).get("task_id") or ""


def spooled(sid):
    d = capture.spool_dir(sid)
    return sorted(p.name for p in d.glob("*.ev")) if d.is_dir() else []


def index_lines(sid):
    p = capture.spool_dir(sid) / "index"
    return p.read_text().splitlines() if p.exists() else []


def jbase(sid, cwd, tp=None, **kw):
    d = {"session_id": sid, "transcript_path": str(tp or (JT / f"{sid}.jsonl")), "cwd": str(cwd)}
    d.update(kw)
    return d


def section(text, name):
    """One section of a summary text ('' when it isn't there)."""
    parts = text.split(f"\n## {name}")
    return parts[1].split("\n## ")[0] if len(parts) > 1 else ""


def dump(name, text):
    """CHECKPOINT_TEST_DUMP=<dir>: keep a summary for a look after the run."""
    d = os.environ.get("CHECKPOINT_TEST_DUMP")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        (Path(d) / f"{name}.txt").write_text(text)


def scan_for(values, root):
    """Files under root holding any of the values (raw or JSON-escaped); names only."""
    hits = []
    forms = [v for val in values for v in (val, json.dumps(val)[1:-1])]
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            p = Path(dp) / fn
            try:
                data = p.read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if any(f in data for f in forms):
                hits.append(str(p.relative_to(root)))
    return hits


# --- the sanitizer ------------------------------------------------------------------------
print("the sanitizer (synthetic secrets, generated here, never printed):")
SECRETS = {
    "github classic": "ghp_" + _rand(36), "github oauth": "gho_" + _rand(36),
    "github fine-grained": "github_pat_" + _rand(22) + "_" + _rand(59), "gitlab": "glpat-" + _rand(20),
    "hugging face": "hf_" + _rand(34), "openai": "sk-proj-" + _rand(48), "anthropic": "sk-ant-api03-" + _rand(90),
    "stripe": "sk_live_" + _rand(24), "slack": "xoxb-" + _rand(11, _string.digits) + "-" + _rand(24),
    "aws key id": "AKIA" + _rand(16, _string.ascii_uppercase + _string.digits),
    "aws session id": "ASIA" + _rand(16, _string.ascii_uppercase + _string.digits),
    "aws secret": _rand(40, _string.ascii_letters + _string.digits + "/+"),
    "google": "AIza" + _rand(35), "npm": "npm_" + _rand(36), "pypi": "pypi-AgEIcHlwaS5vcmc" + _rand(50),
    "jwt": "eyJ" + _rand(24) + ".eyJ" + _rand(40) + "." + _rand(43),
    "password": _rand(18), "token": _rand(32), "basic": _rand(28), "pem body": _rand(64),
    # Names written without separators, lowercase JSON keys, a value with a
    # comma, a short name (DB_PASS), assignments inside another value.
    "pgpassword": _rand(22), "apitoken": _rand(30), "dbpassword": _rand(20), "githubtoken": _rand(30),
    "redispassword": _rand(20), "json pgpassword": _rand(20), "json dbpassword": _rand(20),
    "comma head": _rand(10), "comma tail": _rand(12), "db_pass": _rand(20), "nested": _rand(20),
    "quoted nested": _rand(20), "prose": "Zq" + _rand(16) + "7",
    # A name that is the value of another (`note: PGPASSWORD: x`): 0.5.0.
    "chained yaml": _rand(20), "chained label": _rand(20),
    # Command-line credentials only their program names (CK-SEC-2).
    "mysql -p": _rand(18), "curl -u": _rand(18), "sshpass -p": _rand(18), "docker login -p": _rand(18),
    "redis-cli -a": _rand(18), "sudo -S": _rand(18),
}
S = SECRETS
CORPUS = [
    f"git clone https://{S['github classic']}@github.com/acme/app.git",
    f"export GITHUB_TOKEN={S['github oauth']}", f"GH_TOKEN='{S['github fine-grained']}' gh pr list",
    f"curl -H 'PRIVATE-TOKEN: {S['gitlab']}' https://gitlab.example/api",
    f"huggingface-cli login --token {S['hugging face']}", f"OPENAI_API_KEY={S['openai']} python app.py",
    f'{{"api_key": "{S["anthropic"]}", "model": "x"}}', f"stripe charges create --api-key {S['stripe']}",
    f"SLACK_BOT_TOKEN={S['slack']}", f"aws_access_key_id = {S['aws key id']}",
    f"aws_secret_access_key = {S['aws secret']}", f"AWS_SESSION={S['aws session id']}",
    f"maps?key={S['google']}", f"//registry.npmjs.org/:_authToken={S['npm']}",
    f"twine upload -p {S['pypi']}", f"Cookie: session={S['jwt']}",
    f"psql postgres://admin:{S['password']}@db.internal:5432/prod",
    f"curl -H 'Authorization: Bearer {S['token']}' https://api.example",
    f"Authorization: Basic {S['basic']}", f"mysql -uroot --password={S['password']} app",
    f"DB_PASSWORD: \"{S['password']}\"", f'{{"client_secret":"{S["token"]}","nested":{{"x":1}}}}',
    "-----BEGIN RSA PRIVATE KEY-----\n" + S["pem body"] + "\n" + _rand(64) + "\n-----END RSA PRIVATE KEY-----",
    f"echo {S['github classic']} | docker login ghcr.io -u me --password-stdin",
    f"PGPASSWORD={S['pgpassword']} psql -h db app", f"export APITOKEN={S['apitoken']}",
    f"DBPASSWORD={S['dbpassword']} ./run", f"GITHUBTOKEN={S['githubtoken']} gh api user",
    f"REDISPASSWORD: {S['redispassword']}", f'{{"pgpassword": "{S["json pgpassword"]}"}}',
    f'{{"user":"app","dbpassword":"{S["json dbpassword"]}"}}', f"PASSWORD={S['comma head']},{S['comma tail']} ./x",
    f"DB_PASS={S['db_pass']}", f"opts=a=1:TOKEN={S['nested']}", f"cmd=\"PGPASSWORD={S['quoted nested']} psql\"",
    f"the PGPASSWORD is {S['prose']}",
    f"note: REDISPASSWORD: {S['chained yaml']}", f"config: DB_PASSWORD = {S['chained label']}",
    f"mysql -u root -p{S['mysql -p']} appdb < fixtures.sql", f"curl -u alice:{S['curl -u']} https://api.example.com/v1",
    f"sshpass -p {S['sshpass -p']} ssh deploy@10.0.0.5 uptime", f"docker login -u bob -p {S['docker login -p']} r.io",
    f"redis-cli -h cache -a {S['redis-cli -a']} ping", f"echo {S['sudo -S']} | sudo -S apt-get update",
]
leaks = [i for i, t in enumerate(CORPUS) if any(v in sanitize.scrub(t) for v in SECRETS.values())]
check(f"scrub(): no synthetic secret survives in {len(CORPUS)} contexts (tokens, URLs, headers, env, "
      "JSON, YAML, flags, PEM)", lambda: not leaks)
if leaks:
    print(f"       (leaking contexts: {leaks})")
check("...and what recovery needs stays: commands, hosts, variable names",
      lambda: all(s in sanitize.scrub(t) for t, s in ((CORPUS[0], "git clone https://"), (CORPUS[0], "github.com/acme/app.git"),
                                              (CORPUS[1], "export GITHUB_TOKEN="), (CORPUS[16], "@db.internal:5432/prod"),
                                              (CORPUS[18], "Authorization: Basic"))))
check("...ordinary text is untouched (counts, paths, test commands)",
      lambda: sanitize.scrub("pytest -q tests/ && make build; max_tokens=100 git checkout main")
      == "pytest -q tests/ && make build; max_tokens=100 git checkout main")
_plain = "input_tokens=12 output_tokens=7 maxTokens=3 tokenizer=bpe a=1,b=2 x=y:z pytest -q"
check("...names that merely contain a word are judged as a whole: PGPASSWORD, APITOKEN, GITHUBTOKEN are "
      "sensitive; token counts and a tokenizer are not",
      lambda: all(sanitize.sensitive_name(n) for n in ("PGPASSWORD", "APITOKEN", "DBPASSWORD", "GITHUBTOKEN",
                                                       "REDISPASSWORD", "pgpassword", "DB_PASS", "mysqlPwd"))
      and not any(sanitize.sensitive_name(n) for n in ("max_tokens", "input_tokens", "maxTokens", "tokenizer",
                                                       "num_tokens", "path", "author", "passed"))
      and sanitize.scrub(_plain) == _plain)
_sv = sanitize.scrub_value({"apiKey": "plainlooking", "Authorization": "x y", "max_tokens": 5, "taskId": "1",
                            "nested": [{"client_secret": "abc", "note": f"TOKEN={S['token']}"}]})
check("scrub_value(): fields named key/token/secret/auth lose their value whatever it looks like; counts and ids stay",
      lambda: _sv["apiKey"] == "***" and _sv["Authorization"] == "***" and _sv["max_tokens"] == 5 and _sv["taskId"] == "1"
      and _sv["nested"][0]["client_secret"] == "***" and S["token"] not in json.dumps(_sv))
_ti = sanitize.tool_input("Write", {"file_path": "/p/a.py", "content": f"KEY = '{S['openai']}'\n" * 100})
check("tool_input(Write): the path, size and content hash; never the content",
      lambda: _ti["file_path"] == "/p/a.py" and _ti["content_bytes"] > 1000 and len(_ti["content_sha256"]) == 64
      and "content" not in _ti and S["openai"] not in json.dumps(_ti))
_ti = sanitize.tool_input("Edit", {"file_path": "/p/a.py", "old_string": S["password"], "new_string": "x"})
check("tool_input(Edit): lengths of the strings, never the strings", lambda: S["password"] not in json.dumps(_ti)
      and _ti["old_len"] == len(S["password"]))
_ti = sanitize.tool_input("mcp__other__deploy", {"target": "prod", "secret": S["token"], "blob": "z" * 5000,
                                                 "url": f"https://u:{S['password']}@h/x"})
check("tool_input(unknown MCP tool): a short scrubbed summary only (keys, <= 300 chars), never its raw input",
      lambda: set(_ti) == {"keys", "summary"} and len(_ti["summary"]) <= 300 and S["token"] not in json.dumps(_ti)
      and S["password"] not in json.dumps(_ti) and "zzzzzzzzzz" * 50 not in _ti["summary"])
_tr = sanitize.tool_response("Bash", {"stdout": "x" * 9000 + f" token={S['token']}", "stderr": "", "interrupted": False},
                             output=True)
check("tool_response(Bash) of a test, build or check: a scrubbed bounded tail of the output, and no exit code "
      "(Claude Code's result states none)",
      lambda: "exit" not in _tr and len(_tr["stdout_tail"]) <= 400 and S["token"] not in _tr["stdout_tail"])
_tr = sanitize.tool_response("Bash", {"stdout": "db.internal:5432:*:admin:" + S["password"], "stderr": "",
                                      "interrupted": False})
check("tool_response(Bash) of any other command: the output's size only (a `cat` of a file is its contents)",
      lambda: _tr == {"stdout_chars": 25 + len(S["password"])})
_tr = sanitize.tool_response("Bash", {"stdout": "", "stderr": "", "interrupted": False,
                                      "returnCodeInterpretation": "No matches found"})
check("...a result Claude Code interprets (grep's exit 1): its words kept, still no exit code",
      lambda: _tr == {"interpretation": "No matches found"})
_pg = "db.internal:5432:*:admin:" + S["password"] + "\n"
_tr = sanitize.tool_response("Read", {"type": "text", "file": {"filePath": "/h/.pgpass", "content": _pg, "numLines": 1,
                                                               "startLine": 1, "totalLines": 1}})
check("tool_response(Read): the path, line counts and size; never the content",
      lambda: _tr["file_path"] == "/h/.pgpass" and _tr["lines"] == 1 and _tr["content_chars"] == len(_pg)
      and "db.internal" not in json.dumps(_tr) and S["password"] not in json.dumps(_tr))
_tr = sanitize.tool_response("Grep", {"mode": "content", "numFiles": 1, "filenames": ["/h/.env"], "content": _pg,
                                      "numLines": 1})
_tw = sanitize.tool_response("WebFetch", {"code": 200, "codeText": "OK", "bytes": 900, "result": _pg,
                                          "url": "https://h/x?k=v"})
_tm = sanitize.tool_response("mcp__other__read_secret", {"value": _pg, "path": "/h/.pgpass"})
check("tool_response(Grep, WebFetch, an unknown tool): counts, paths, sizes, keys; never matched lines, page text "
      "or a value", lambda: S["password"] not in json.dumps([_tr, _tw, _tm]) and _tr["paths"] == ["/h/.env"]
      and _tr["content_chars"] == len(_pg) and _tw["code"] == 200 and "k=v" not in json.dumps(_tw)
      and _tm == {"keys": ["path", "value"], "chars": len(json.dumps({"value": _pg, "path": "/h/.pgpass"}))})

# Command-line credentials whose flag says nothing by itself (`-p`, `-a`,
# `-u`, `-k`): masked after the program that reads them that way (CK-SEC-2).
_PW = "Q" + _rand(14) + "7"


def _cli_ok(cases):
    """Every case loses the canary and keeps what is given as kept."""
    outs = [(sanitize.scrub(c.replace("PW", _PW)), keep) for c, keep in cases]
    return all(_PW not in o and keep in o for o, keep in outs)


check("scrub(): mysql/mariadb/mysqladmin -p<pw> and -p <pw> lose the password; the database name stays",
      lambda: _cli_ok([("mysql -u root -pPW appdb < fixtures.sql", "-p*** appdb < fixtures.sql"),
                       ("mysql -uroot -p'PW' mydb", "-p'***' mydb"), ("mysql -u root -p PW prod", "-p *** prod"),
                       ("mysqladmin -u root -pPW status", "-p*** status"),
                       ("mariadb -h db -P 3306 -pPW app", "-P 3306 -p*** app"),
                       ("docker exec -it db mysql -uroot -pPW app", "mysql -uroot -p*** app")]))
check("scrub(): curl -u/--user user:pw (and --proxy-user, wget --user=) keep the user, lose the password",
      lambda: _cli_ok([("curl -u alice:PW https://api.example.com/v1/items", "curl -u alice:*** https://api"),
                       ("curl --user alice:PW https://x", "--user alice:***"), ("curl --user=alice:PW x", "--user=alice:***"),
                       ("curl -ualice:PW x", "-ualice:***"), ("curl -U proxy:PW -x p:3128 x", "-U proxy:***"),
                       ("wget --user=alice:PW https://x", "--user=alice:***")]))
check("scrub(): sshpass -p (attached or not) and SSHPASS= lose the password; ssh's own -p stays",
      lambda: _cli_ok([("sshpass -p PW ssh -p 2222 deploy@10.0.0.5 uptime", "sshpass -p *** ssh -p 2222 deploy@"),
                       ("sshpass -p'PW' scp -P 22 a b:", "sshpass -p'***' scp -P 22"),
                       ("SSHPASS=PW sshpass -e ssh h", "SSHPASS=*** sshpass -e")]))
check("scrub(): docker/podman/helm/nerdctl/az login -p/--password lose the password",
      lambda: _cli_ok([("docker login -u bob -p PW registry.example.com", "-u bob -p *** registry.example.com"),
                       ("podman login -pPW -u x q.io", "-p*** -u x"), ("helm registry login r.io -u x -p PW", "-p ***"),
                       ("nerdctl login --password PW r", "--password ***"), ("az login -u a@b.c -p PW", "-u a@b.c -p ***")]))
check("scrub(): redis-cli -a, htpasswd -b, openssl -k / -K / -pass pass: lose the password",
      lambda: _cli_ok([("redis-cli -h h -p 6379 -a PW ping", "-p 6379 -a *** ping"),
                       ("htpasswd -b .htpasswd alice PW", "htpasswd -b .htpasswd alice ***"),
                       ("htpasswd -nbB alice PW", "-nbB alice ***"),
                       ("openssl enc -aes-256-cbc -k PW -in a -out b", "-k *** -in a"),
                       ("openssl enc -aes-256-cbc -K 0011PW -iv 00", "-K *** -iv 00"),
                       ("openssl pkcs12 -export -passout pass:PW -out x.p12", "pass:*** -out x.p12")]))
check("scrub(): what echo/printf pipe into sudo -S (or --password-stdin, chpasswd) and sudo -S <<< lose it",
      lambda: _cli_ok([("echo PW | sudo -S apt-get update", "echo *** | sudo -S apt-get update"),
                       ("echo 'PW' | sudo -u root -S ls", "| sudo -u root -S ls"),
                       ("step 2: echo PW | sudo -S true", "step 2: echo *** | sudo -S true"),
                       ("printf '%s\\n' PW | sudo -S -k true", "| sudo -S -k true"), ("sudo -S true <<< PW", "<<< ***"),
                       ("echo PW | docker login -u x --password-stdin", "echo *** | docker login"),
                       ("echo 'u:PW' | sudo chpasswd", "echo *** | sudo chpasswd")]))
# Its last character drawn apart, so every run checks each ending (a random one ended in "-" now and then and
# the bare form leaked: round 2's flake, 1 run in ~75).
_bots = ["123456789:AA" + _rand(32, _string.ascii_letters + _string.digits + "_-") + _end for _end in "-_x7"]
_bot = _bots[2]
check("scrub(): a Telegram bot token (in its API URL, or bare) is masked; the URL's host and method stay",
      lambda: _bot not in sanitize.scrub(f"curl https://api.telegram.org/bot{_bot}/sendMessage")
      and "api.telegram.org/bot123456789:***/sendMessage" in sanitize.scrub(f"curl https://api.telegram.org/bot{_bot}/sendMessage")
      and _bot.split(":")[1] not in sanitize.scrub(f"notify {_bot}"))
check("...also a bare token whose last character is a dash or an underscore (no word boundary after it), at the "
      "end of the text or before a space",
      lambda: all(b.split(":")[1] not in sanitize.scrub(f"notify {b}") and b.split(":")[1] not in sanitize.scrub(f"{b} sent")
                  for b in _bots))
_near = ["ssh -p 2222 host", "scp -P 22 a host:b", "mkdir -p a/b", "cp -p a b", "git log -p -3", "tar -czpf x.tgz d",
         "pytest -k 'test_x and not slow' -q", "sort -k2 f", "docker run -p 8080:80 nginx", "redis-cli -p 6379 ping",
         "curl -u alice https://x", "echo hi | sudo tee /x", "echo y | sudo pacman -S foo", "mysql -h db -P 3306 app",
         "openssl rsa -passin env:PW -in k", "htpasswd -D f u", "wget -U 'Mozilla/5.0 rv:109.0' x",
         "git commit -m 'fix mysql -p parsing'", "rsync -avzP a b:"]
check("...near misses stay untouched (ssh -p, scp -P, mkdir -p, cp -p, git log -p, tar -czpf, pytest -k, docker run -p, "
      "sudo pacman -S ...)", lambda: all(sanitize.scrub(c) == c for c in _near))
# After a separator (round 2's must-fix: each later segment's words were
# placed at the wrong offsets, so the canary stayed and the command broke).
_after_sep = [("cd /srv && mysql -u root -pPW app", "cd /srv && mysql -u root -p*** app"),
              ("set -e; docker login -u bob -p PW r.io && docker push r.io/x",
               "set -e; docker login -u bob -p *** r.io && docker push r.io/x"),
              ("echo start && echo PW | sudo -S true", "echo start && echo *** | sudo -S true"),
              ("ls -la | mysql -pPW db", "ls -la | mysql -p*** db"), ("(cd x && mysql -pPW)", "(cd x && mysql -p***)"),
              ("export A=1\nmysql -pPW db", "export A=1\nmysql -p*** db"),
              ("x=1; sshpass -p 'PW' ls", "x=1; sshpass -p '***' ls"),
              ("cd ~/p; curl -u alice:PW https://x/v1 && git push", "cd ~/p; curl -u alice:*** https://x/v1 && git push")]
check("scrub(): a credential after a separator (&&, ;, |, (, a newline) is masked in place: exactly the command "
      "back with only the value masked",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _after_sep))
_push = sanitize.tool_input("Bash", {"command": f"cd /home/u/p && docker login -u bob -p {_PW} reg.io && docker push "
                                                f"reg.io/app:1"})
check("...so the journaled command of a scrubbed login-and-push is still an external effect (deploy) for 'Check first'",
      lambda: _PW not in _push["command"] and __import__("checkpoint.recovery").recovery.external_effect(
          "Bash", _push) == "deploy")
check("scrub(): a command line quoted inside another (ssh host '...', sh -c, bash -lc, docker exec ... sh -c, "
      "kubectl exec -- bash -c, su -c, eval, nested) loses its credentials; the rest stays",
      lambda: _cli_ok([("ssh host 'mysql -u root -pPW app'", "ssh host 'mysql -u root -p*** app'"),
                       ('ssh -p 2222 -i k u@h "cd /srv && mysql -pPW app"', 'ssh -p 2222 -i k u@h "cd /srv && mysql -p*** app"'),
                       ('docker exec db sh -c "mysql -uroot -pPW app"', 'docker exec db sh -c "mysql -uroot -p*** app"'),
                       ('bash -c "mysql -pPW"', 'bash -c "mysql -p***"'),
                       ("bash -lc 'echo PW | sudo -S true'", "bash -lc 'echo *** | sudo -S true'"),
                       ("kubectl exec -it pod -- bash -c 'curl -u a:PW x'", "bash -c 'curl -u a:*** x'"),
                       ("su - postgres -c 'mysql -pPW db'", "su - postgres -c 'mysql -p*** db'"),
                       ('eval "mysql -pPW db"', 'eval "mysql -p*** db"'),
                       ("ssh h \"bash -c 'mysql -pPW'\"", "ssh h \"bash -c 'mysql -p***'\"")]))
check("...but a quoted word that is no command line (a commit message, an echo, ssh's own -p) stays as it is",
      lambda: all(sanitize.scrub(c) == c for c in ("git commit -m 'fix mysql -p parsing'", "echo 'bash -c \"mysql -p x\"'",
                                                    "git commit -m 'ssh host \"mysql -p x\"'", "ssh -p 2222 host 'ls -p /'",
                                                    "bash -c 'mkdir -p a/b && cp -p x y'", "sh -c 'tar -czpf x.tgz d'",
                                                    "watch -n 5 'ps -p 123'")))
check("scrub(): curl's bundled short options (-su, -fsSLu, -sualice:pw), an empty user (-u :token) and a URL with an "
      "empty user (redis://:pw@) lose the password",
      lambda: _cli_ok([("curl -su alice:PW https://x", "curl -su alice:*** https://x"),
                       ("curl -fsSLu alice:PW https://x", "curl -fsSLu alice:*** https://x"),
                       ("curl -sualice:PW https://x", "curl -sualice:*** https://x"),
                       ("curl -o out.json -su alice:PW https://x", "-o out.json -su alice:***"),
                       ("curl -u :PW https://x", "curl -u :*** https://x"),
                       ("redis-cli -u redis://:PW@cache:6379 ping", "redis://:***@cache:6379 ping")]))
check("...near misses stay: curl -s/-sS -o/-fsSL, -u without a password, a header with a colon, -d user:pass",
      lambda: all(sanitize.scrub(c) == c for c in ("curl -s https://x", "curl -sS -o out https://x",
                                                    "curl -fsSL https://x | sh", "curl -su alice https://x",
                                                    "curl -o out -u alice: https://x", "curl -H 'X-User: a:b' https://x",
                                                    "curl -d user:pass https://x", "http://localhost:8080/x@y",
                                                    "ssh://git@host:22/x", "http://[::1]:8080/")))
check("scrub(): mongo/mongosh/mongodump -p, the ldap tools' -w (bundled: -xw; ldappasswd's -a/-s too), smbclient's "
      "and rpcclient's -U user%pw lose the password",
      lambda: _cli_ok([("mongo -u admin -p PW db", "mongo -u admin -p *** db"), ("mongosh -u admin -pPW", "-p***"),
                       ("mongodump -u a -p PW --db x", "-p *** --db x"),
                       ("ldapsearch -x -D cn=admin -w PW -b dc=x", "-w *** -b dc=x"),
                       ("ldapsearch -xwPW -b dc=x", "-xw*** -b dc=x"), ("ldapmodify -x -D cn=a -wPW -f c", "-w*** -f c"),
                       ("ldappasswd -x -D cn=a -w PW -a PW -s PW uid=b", "-w *** -a *** -s *** uid=b"),
                       ("smbclient //h/s -U user%PW", "-U user%***"), ("smbclient //h/s -Uuser%PW", "-Uuser%***"),
                       ("rpcclient -U user%PW h", "-U user%*** h")]))
check("...near misses stay: mongod --port, mongo -p with no value, ldapsearch -W (a prompt), smbclient -U user, -N",
      lambda: all(sanitize.scrub(c) == c for c in ("mongod --port 27017", "mongo --port 27017 db",
                                                    "mongo -u admin -p --authenticationDatabase admin",
                                                    "ldapsearch -x -W -b dc=x", "ldapsearch -x -H ldap://h -b dc=x uid=w",
                                                    "smbclient -L h -N", "smbclient //h/s -U user",
                                                    "net -U user rpc info")))
check("scrub(): `login` counts only as the subcommand (docker run -p 8080:80 img login stays; docker --config d login, "
      "helm registry login and az acr login lose -p)",
      lambda: all(sanitize.scrub(c) == c for c in ("docker run -p 8080:80 img login", "podman run -p 5000:5000 reg login",
                                                    "docker push -q r/login"))
      and _cli_ok([("docker --config /x login -u bob -p PW r.io", "--config /x login -u bob -p *** r.io"),
                   ("helm registry login r.io -u x -p PW", "-p ***"), ("az acr login -n reg -u x -p PW", "-u x -p ***")]))
# Round 3: what a command substitution runs is read again (round 2's tokenizer kept `"$(...)"` as one word
# and never looked inside it: 9 of 10 forms leaked), and the inner command lines round 2 still missed.
_subst = [('n="$(mysql -uroot -pPW -N -e "select 1" db)"', 'n="$(mysql -uroot -p*** -N -e "select 1" db)"'),
          ('out="$(curl -u alice:PW https://x)"', 'out="$(curl -u alice:*** https://x)"'),
          ('echo "$(redis-cli -a PW ping)"', 'echo "$(redis-cli -a *** ping)"'),
          ('r="$(docker login -u b -p PW r.io)"', 'r="$(docker login -u b -p *** r.io)"'),
          ('echo "$(echo PW | sudo -S id)"', 'echo "$(echo *** | sudo -S id)"'),
          ('v="$(sshpass -p PW ssh h uptime)"', 'v="$(sshpass -p *** ssh h uptime)"'),
          ("echo \"rows: $(mysql -pPW -e 'select count(*) from t' db)\"",
           "echo \"rows: $(mysql -p*** -e 'select count(*) from t' db)\""),
          ('if [ "$(mysql -pPW -e "x")" = 1 ]; then :; fi', 'if [ "$(mysql -p*** -e "x")" = 1 ]; then :; fi'),
          ('logger "$(htpasswd -nb alice PW)"', 'logger "$(htpasswd -nb alice ***)"'),
          ("x=`mysql -pPW -e 'select 1'`", "x=`mysql -p*** -e 'select 1'`"),
          ('echo "`mysql -pPW`"', 'echo "`mysql -p***`"'),
          ("x=$(mysql -pPW -e 'select 1')", "x=$(mysql -p*** -e 'select 1')"),
          ('echo "$(cat "$(curl -u a:PW https://x)")"', 'echo "$(cat "$(curl -u a:*** https://x)")"'),
          ('ssh h "x=\\$(mysql -pPW)"', 'ssh h "x=\\$(mysql -p***)"'),
          ('echo "$(mysql -pPW', 'echo "$(mysql -p***')]
check("scrub(): what a command substitution runs ($(...) and backticks, inside double quotes too, nested, one not "
      "closed) loses its credentials: exactly the command back with only the value masked",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _subst))
check("...but text that only looks like one stays: a single-quoted $(...), arithmetic, awk's $(NF), a "
      "substitution without credentials, a commit message naming a program",
      lambda: all(sanitize.scrub(c) == c for c in ("echo '$(mysql -p x)'", "echo $((1 + 2))", "awk '{print $(NF)}' f",
                                                    'echo "$(date)"', 'cd "$(git rev-parse --show-toplevel)"',
                                                    "kill $(lsof -t -i:8080)", 'sh -c "$(curl -fsSL https://x/i.sh)"',
                                                    'echo "user: `whoami` on $(hostname)"',
                                                    'git commit -m "fix: $(mysql) parsing"')))
_unclosed = [("I can't log in: mysql -u root -pPW app", "I can't log in: mysql -u root -p*** app"),
             ("bash -c 'mysql -pPW", "bash -c 'mysql -p***"), ('ssh h "mysql -pPW', 'ssh h "mysql -p***'),
             ('say "hello && curl -u a:PW https://x', 'say "hello && curl -u a:*** https://x')]
check("...and a quote that is never closed (an apostrophe in prose, a cut command) never hides the rest of the text: "
      "the words after it are read as before",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _unclosed))
_inner3 = [("bash -o pipefail -c 'mysql -pPW db'", "bash -o pipefail -c 'mysql -p*** db'"),
           ('bash -euo pipefail -c "curl -u a:PW https://x"', 'bash -euo pipefail -c "curl -u a:*** https://x"'),
           ("bash -O extglob -c 'mysql -pPW'", "bash -O extglob -c 'mysql -p***'"),
           ("sh -c -- 'mysql -pPW'", "sh -c -- 'mysql -p***'"),
           ("bash --norc -c 'mysql -pPW'", "bash --norc -c 'mysql -p***'"),
           ('ssh host "bash -c \\"mysql -pPW\\""', 'ssh host "bash -c \\"mysql -p***\\""'),
           ('ssh h "mysql -p\\"PW\\" db"', 'ssh h "mysql -p\\"***\\" db"'),
           ("ssh h 'bash -c \"sh -c \\\"mysql -pPW\\\"\"'", "ssh h 'bash -c \"sh -c \\\"mysql -p***\\\"\"'"),
           ('script -qc "mysql -pPW" /dev/null', 'script -qc "mysql -p***" /dev/null'),
           ("su bob --command='mysql -pPW'", "su bob --command='mysql -p***'"),
           ('vagrant ssh -c "mysql -pPW"', 'vagrant ssh -c "mysql -p***"'),
           ("bash -c $'mysql -pPW'", "bash -c $'mysql -p***'"),
           ("tmux new-session -d 'mysql -pPW'", "tmux new-session -d 'mysql -p***'")]
check("scrub(): more command lines quoted in another: a shell's options before -c (-o pipefail, -euo pipefail, -O, "
      "--norc, -c --), escaped quotes inside ssh \"...\", script -qc, su --command=, vagrant ssh -c, $'...', tmux",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _inner3))
check("...near misses stay: bash -o vi, bash -x script.sh, script -q file cmd, tmux send-keys 'ls -p', an escaped "
      "quote with no credential",
      lambda: all(sanitize.scrub(c) == c for c in ("bash -o vi", "bash -x script.sh arg", "sh -e ./x.sh",
                                                    "script -q /dev/null ls -la", "su - bob",
                                                    "tmux send-keys -t dev 'ls -p' Enter",
                                                    "tmux new-session -d 'make -j4 && make install'",
                                                    'ssh h "echo \\"hello world\\""')))
_more3 = [("curl -E cert.pem:PW https://x", "curl -E cert.pem:*** https://x"),
          ("curl --cert cert.pem:PW https://x", "curl --cert cert.pem:*** https://x"),
          ("curl -sE 'c.pem:PW' https://x", "curl -sE 'c.pem:***' https://x"),
          ("curl --cert=c.pem:PW https://x", "curl --cert=c.pem:*** https://x"),
          ("smbclient //srv/share PW -U user", "smbclient //srv/share *** -U user"),
          ("smbclient -U user '//srv/share' PW", "smbclient -U user '//srv/share' ***"),
          ("podman --cgroup-manager systemd login -p PW r", "podman --cgroup-manager systemd login -p *** r"),
          ("podman --remote login -p PW r", "podman --remote login -p *** r"),
          ("podman --log-level debug --events-backend file login -u x -p PW r",
           "podman --log-level debug --events-backend file login -u x -p *** r")]
check("scrub(): curl's -E/--cert file:password, smbclient's password after the share, and podman's global options "
      "before login (any long one with its value) lose the password",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _more3))
check("...near misses stay: curl -E with no password, a pkcs11 URI, smbclient -c/-N after the share, podman --remote "
      "ps, a docker run whose image is called login",
      lambda: all(sanitize.scrub(c) == c for c in ("curl -E cert.pem https://x", "curl --cert c.pem https://x",
                                                    "curl -E 'pkcs11:object=k' https://x",
                                                    "smbclient //h/s -c 'ls'", "smbclient //h/s -N",
                                                    "smbclient -L //h -U user", "podman --remote ps",
                                                    "docker --context prod run -p 8080:80 img login",
                                                    "docker --tls run --rm -p 8080:80 img login")))
_cont = [("curl -u \\\n  alice:PW https://x", "curl -u \\\n  alice:*** https://x"),
         ("curl --user \\\n  alice:PW https://x", "curl --user \\\n  alice:*** https://x"),
         ("sshpass \\\n  -p PW ssh h", "sshpass \\\n  -p *** ssh h"),
         ("docker login -u bob -p \\\n  PW r.io", "docker login -u bob -p \\\n  *** r.io"),
         ("mysql -u root \\\n  -pPW app", "mysql -u root \\\n  -p*** app")]
check("scrub(): a line continuation (backslash-newline) between an option and its value is a space: the value is "
      "masked, never the backslash",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _cont))
_t0 = time.time()
_long = [sanitize.scrub("curl " + "a" * 100000), sanitize.scrub("curl -u x " + "a-" * 50000),
         sanitize.scrub("printf %s " + "0123456789abcdef" * 6250 + " TOKEN=" + _PW),
         sanitize.scrub("x " + "a." * 50000 + " --password " + _PW),
         sanitize.scrub("we use curl, it's fine; " * 10 + "curl it's " * 20000 + "curl -u a:" + _PW)]
_long_s = time.time() - _t0
check("scrub(): a 100,000-character word costs its length, not its square (a word with a hint, dashes, hex, dots; a "
      "line naming curl 20,000 times: under 10 s together; round 2 took ~96 s for the first alone), and an "
      "assignment, flag or credential after it still loses its value",
      lambda: _long_s < 10 and _PW not in _long[2] and _PW not in _long[3] and "TOKEN=***" in _long[2]
      and _PW not in _long[4] and _long[4].endswith("curl -u a:***"))
# Round 4 (round 3's must-fix): an apostrophe in a comment, a here-document's body or prose paired with a later
# quote and hid every command line between (round 3 read inside Claude Code's own commit form and regressed it).
_cmt = [("# Don't print the password\nmysql -u root -pPW -e 'select 1' db",
         "# Don't print the password\nmysql -u root -p*** -e 'select 1' db"),
        ("cd /srv  # don't forget\ncurl -u admin:PW https://x/api -d '{}'",
         "cd /srv  # don't forget\ncurl -u admin:*** https://x/api -d '{}'"),
        ("set -e\n# it's prod, be careful\ndocker login -u bob -p PW r.io\ndocker push r.io/app:latest\necho 'done'",
         "set -e\n# it's prod, be careful\ndocker login -u bob -p *** r.io\ndocker push r.io/app:latest\necho 'done'"),
        ("# Ask O'Neil first\ngit commit -m \"notes\n\nmore\" && mysql -pPW -e \"select 'a'\"",
         "# Ask O'Neil first\ngit commit -m \"notes\n\nmore\" && mysql -p*** -e \"select 'a'\""),
        ("# mysql -u root -pPW app\nls", "# mysql -u root -p*** app\nls"), ("ls  # mysql -pPW", "ls  # mysql -p***")]
check("scrub(): a # comment ends at its newline as in the shell: its apostrophe hides no later line (exactly the "
      "text back with only the value masked), and a commented-out credential command still loses its value",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _cmt))
_hd = [("cat > NOTES.md <<'EOF'\nDon't commit the password.\nEOF\nmysql -uroot -pPW app -e 'select 1'",
        "cat > NOTES.md <<'EOF'\nDon't commit the password.\nEOF\nmysql -uroot -p*** app -e 'select 1'"),
       ("cat > NOTES.md <<EOF\nIt's $(date)\nEOF\nmysql -uroot -pPW -e 'select 1' app",
        "cat > NOTES.md <<EOF\nIt's $(date)\nEOF\nmysql -uroot -p*** -e 'select 1' app"),
       ("cat <<-EOF\n\tO'Reilly\n\tEOF\nmysql -pPW -e 'select 1'", "cat <<-EOF\n\tO'Reilly\n\tEOF\nmysql -p*** -e 'select 1'"),
       ("cat << EOF\nO'Reilly\nEOF\nmysql -pPW -e 'select 1'", "cat << EOF\nO'Reilly\nEOF\nmysql -p*** -e 'select 1'"),
       ('cat <<"EOF"\nO\'Reilly\nEOF\nmysql -pPW -e \'select 1\'', 'cat <<"EOF"\nO\'Reilly\nEOF\nmysql -p*** -e \'select 1\''),
       ("cat <<A <<'B'\nO'R\nA\nit's 'x\nB\nmysql -pPW -e 'select 1'", "cat <<A <<'B'\nO'R\nA\nit's 'x\nB\nmysql -p*** -e 'select 1'"),
       ("cat <<EOF && mysql -pPW db\nO'Reilly\nEOF\necho 'x'", "cat <<EOF && mysql -p*** db\nO'Reilly\nEOF\necho 'x'"),
       ("cat > notes.txt <<'EOF'\nAsk O'Neil first\nEOF\ngit commit -m \"Add notes\n\nMore\" && mysql -pPW -e \"select 'a'\"",
        "cat > notes.txt <<'EOF'\nAsk O'Neil first\nEOF\ngit commit -m \"Add notes\n\nMore\" && mysql -p*** -e \"select 'a'\""),
       # What an unquoted body runs is still read (its quotes are characters there).
       ("cat <<EOF\nHi O'Neil, rows: $(mysql -pPW -e 'select count(*) from t' db)\nEOF",
        "cat <<EOF\nHi O'Neil, rows: $(mysql -p*** -e 'select count(*) from t' db)\nEOF"),
       # A script a here-document writes: its lines are still read on their own.
       ("cat > deploy.sh <<'EOF'\nmysql -u root -pPW app < dump.sql\nEOF\nchmod +x deploy.sh",
        "cat > deploy.sh <<'EOF'\nmysql -u root -p*** app < dump.sql\nEOF\nchmod +x deploy.sh"),
       ("use cat <<EOF to write it\nmysql -pPW app", "use cat <<EOF to write it\nmysql -p*** app")]
check("scrub(): a here-document's body (<<WORD, <<-WORD, << WORD, <<'WORD', <<\"WORD\", two on a line, one never "
      "terminated) is skipped as the shell does: exactly the text back with only the value masked; an unquoted body's "
      "$(...) is still read, and a credential line inside a body still loses its value",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _hd))
# Round 4's review: a credential on a line continued with a backslash inside a here-document's body (one option per
# line, a script written through cat or sent to `ssh host bash`) leaked once the body was skipped.
_hdc = [("ssh db1 'bash -s' <<'EOF'\ncurl -fsS \\\n  -u admin:PW \\\n  https://h/\nEOF",
         "ssh db1 'bash -s' <<'EOF'\ncurl -fsS \\\n  -u admin:*** \\\n  https://h/\nEOF"),
        ("cat > deploy.sh <<'EOF'\nmysql -u root \\\n  -pPW app\nEOF\nchmod +x deploy.sh",
         "cat > deploy.sh <<'EOF'\nmysql -u root \\\n  -p*** app\nEOF\nchmod +x deploy.sh"),
        ("cat <<-EOF > x.sh\n\tsshpass -p \\\n\t  PW ssh web1 uptime\n\tEOF",
         "cat <<-EOF > x.sh\n\tsshpass -p \\\n\t  *** ssh web1 uptime\n\tEOF"),
        ("ssh db1 <<EOF\ndocker login \\\n  -u bob -p PW r.io\nEOF", "ssh db1 <<EOF\ndocker login \\\n  -u bob -p *** r.io\nEOF"),
        ("bash -c \"$(cat <<'EOF'\nmysql -u root \\\n  -pPW app\nEOF\n)\"",
         "bash -c \"$(cat <<'EOF'\nmysql -u root \\\n  -p*** app\nEOF\n)\"")]
_hdc_benign = ["docker run --rm \\\n  -p 8080:80 \\\n  -v \"$PWD:/src\" img",
               "cat > build.sh <<'EOF'\ncmake -S . -B build \\\n  -DCMAKE_BUILD_TYPE=Release\nEOF",
               "tar -czpf out.tgz \\\n  src/ docs/", "ssh -p 2222 \\\n  web1 'ls -p /srv'",
               "sed -e's,a,b,' -e's,c,d,' f.txt", "awk -F'\\t' '{print $1}' f.tsv",
               "docker run -v \"$PWD:/src\" -w /src img make", "cp $PWD:/x y"]
check("scrub(): a variable's value followed by text ($PWD:/src) is no NAME: value, while ${DB_PASS:-default} and "
      "PWD: value still lose the value",
      lambda: sanitize.scrub("docker run -v \"$PWD:/src\" img") == "docker run -v \"$PWD:/src\" img"
      and _PW not in sanitize.scrub("x=${DB_PASS:-" + _PW + "}") and _PW not in sanitize.scrub("PWD: " + _PW))
# An option's quoted value glued to it (-e's,a,b,', -d't ') is a quote, not a contraction: read as one, it paired
# with a later quote and hid the credential command between (found by the 0.8.0 verification's mutation).
_optq = [("sed -e's,a,b,' f && mysql -pPW -e 'select 1'", "sed -e's,a,b,' f && mysql -p*** -e 'select 1'"),
         ("sed -e's,a,b,' f.txt && curl -u admin:PW https://x -d '{}'",
          "sed -e's,a,b,' f.txt && curl -u admin:*** https://x -d '{}'"),
         ("cut -d't ' -f1 f && mysql -uroot -pPW -e 'select 1'", "cut -d't ' -f1 f && mysql -uroot -p*** -e 'select 1'")]
check("scrub(): an option's quoted value glued to it (-e's,a,b,', -d't ') is read as a quote, so a credential "
      "command after it still loses its value (exactly the text back with only the value masked)",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _optq))
check("scrub(): a credential on a backslash-continued line inside a here-document's body loses its value (exactly the "
      "text back with only the value masked), while continued commands with no credential, and an option's quoted value "
      "glued to it (-e's,a,b,', -F'\\t'), come back unchanged",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _hdc)
      and all(sanitize.scrub(c) == c for c in _hdc_benign))
_commit ="git commit -m \"$(cat <<'EOF'\n{msg}\n\nCo-Authored-By: x <y@z>\nEOF\n)\" && "
_cf = [(_commit.format(msg="fix: don't drop the table") + "mysql -uroot -pPW -e 'select 1'", "mysql -uroot -p*** -e 'select 1'"),
       (_commit.format(msg="fix: don't drop the table") + "curl -u ci:PW -X POST https://ci/api -d '{}'",
        "curl -u ci:*** -X POST https://ci/api -d '{}'"),
       (_commit.format(msg="fix: don't drop it, it's needed") + "sshpass -p PW ssh h 'uptime'", "sshpass -p *** ssh h 'uptime'"),
       (_commit.format(msg="Update O'Reilly link") + "curl -u ci:PW -H \"X-A: b\" -d '{}' https://x",
        "curl -u ci:*** -H \"X-A: b\" -d '{}' https://x")]
check("scrub(): Claude Code's commit form (git commit -m \"$(cat <<'EOF' ... EOF\\n)\" && ...) with an apostrophe in "
      "the message (a contraction, or a name's: O'Reilly, with a `)\"` followed by another quote on its line): "
      "exactly the text back with only the value masked",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == c[:c.index("&& ") + 3] + want for c, want in _cf))
_ml = [("I'm getting an error when I run\nmysql -u root -pPW app\nIt's saying access denied",
        "I'm getting an error when I run\nmysql -u root -p*** app\nIt's saying access denied"),
       ("It's failing. I ran:\nmysql -u root -pPW app\nand it didn't connect",
        "It's failing. I ran:\nmysql -u root -p*** app\nand it didn't connect"),
       ("Ask O'Neil about it:\nmysql -u root -pPW app\nthe 'prod' one", "Ask O'Neil about it:\nmysql -u root -p*** app\nthe 'prod' one"),
       ("Use this:\n```\nmysql -u root -pPW app\n```\nThat's the prod one.",
        "Use this:\n```\nmysql -u root -p*** app\n```\nThat's the prod one.")]
check("scrub(): a multi-line prompt with apostrophes (contractions, a name's) around a command loses its credential: "
      "each line is read again on its own; exactly the text back with only the value masked",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _ml))
_ctr = [("I can't log in with mysql -u root -pPW app, it's denied", "I can't log in with mysql -u root -p*** app, it's denied"),
        ("I can't log in with mysql -u root -pPW app, it's saying access denied",
         "I can't log in with mysql -u root -p*** app, it's saying access denied"),
        ("why doesn't `curl -u admin:PW https://x` work? it's returning 401",
         "why doesn't `curl -u admin:*** https://x` work? it's returning 401"),
        ("I'd say we're fine: I've run sshpass -p PW ssh h uptime and you'll see I'm right",
         "I'd say we're fine: I've run sshpass -p *** ssh h uptime and you'll see I'm right"),
        ("Ask O'Neil: mysql -u root -pPW app, it can't connect", "Ask O'Neil: mysql -u root -p*** app, it can't connect"),
        ("sed -e's/a/b/' f && mysql -pPW -e 'select 1'", "sed -e's/a/b/' f && mysql -p*** -e 'select 1'")]
check("scrub(): on one line, a contraction's apostrophe (can't, it's, I'd, we're, I've, you'll, I'm: after a letter, "
      "then a space, punctuation or the end) opens no quote and closes none, so the command between two of them, or "
      "between a name's apostrophe and one (O'Neil ... can't), loses its credential; sed -e's/a/b/' stays a quote",
      lambda: all(sanitize.scrub(c.replace("PW", _PW)) == want for c, want in _ctr))
check("...near misses stay as they are: the commit form, a here-document, a here-string, arithmetic <<, a comment "
      "naming a program, `#` inside a word, sed -e's/../', grep -e't', cut -d' ', a quoted it's",
      lambda: all(sanitize.scrub(c) == c for c in (
          _commit.format(msg="fix: keep the table")[:-4], _commit.format(msg="fix: don't drop the table")[:-4],
          "cat <<EOF\nhello\nEOF", "cat <<< \"$x\"", "echo $((1 << 2))", "x=1 # a comment naming mysql",
          "echo a#b && curl https://x/#frag", "sed -e's/a/b/' f", "grep -e't' f", "cut -d' ' -f1 f",
          "printf '%s\\n' \"it's\"", "git commit -m 'fix mysql -p parsing'")))
# Should-fix 1 and 2 of round 3: what was still quadratic (each form's own size; round 3 took 2-10 s each).
_quad = {"su": "su " * 24000 + "-c 'mysql -pPW'", "script": "script " * 24000 + "-c 'mysql -pPW'",
         "sh -o": "sh -o " * 8000 + "; sh -c 'mysql -pPW'", "ssh -p": "ssh -p " * 12000 + "h; ssh h 'mysql -pPW'",
         "sudo -u": "sudo -u " * 8000 + "x; echo PW | sudo -S true", "vagrant": "vagrant ssh " * 16000 + "-c 'mysql -pPW'",
         "url scheme": "x " + "a+" * 50000 + " https://u:PW@h"}
_quad_s = {}
for _k, _t in _quad.items():
    _t0 = time.time()
    _quad[_k] = sanitize.scrub(_t.replace("PW", _PW))
    _quad_s[_k] = time.time() - _t0
check("scrub(): su/script/a shell's -o/ssh -p/sudo -u/vagrant ssh repeated thousands of times on one line, and a "
      "100,000-character word glued to :// cost their length (each under 2.5 s; round 3: 2-10 s), and the credential "
      "after them still loses its value",
      lambda: all(s < 2.5 for s in _quad_s.values()) and not any(_PW in o for o in _quad.values()))
if any(s >= 2.5 for s in _quad_s.values()):
    print(f"       (seconds: { {k: round(v, 2) for k, v in _quad_s.items()} })")
_memo_cap = sanitize._MEMO_CHARS
sanitize._MEMO_CHARS = 5000      # (16 M characters in use; scaled down here)
try:
    with sanitize.remembered():
        _big = [sanitize.scrub(f"{i:03d} TOKEN={_PW} " + "y z " * 50) for i in range(30)]
        _again = [sanitize.scrub(f"{i:03d} TOKEN={_PW} " + "y z " * 50) for i in range(30)]
        _memo_n, _memo_chars = len(sanitize._MEMO), sanitize._memo_chars
finally:
    sanitize._MEMO_CHARS = _memo_cap
check("sanitize.remembered(): the memo is bounded by the total size of its texts and answers too (30 texts of ~230 "
      "characters against 5000: some not kept), every answer still right, all of it gone at the end",
      lambda: 0 < _memo_n < 30 and _memo_chars <= 5000 and _big == _again
      and all(_PW not in b and "TOKEN=***" in b for b in _big) and sanitize._MEMO is None)

_lib = (ROOT / "hooks" / "lib.sh").read_text()
check("the shell and Python agree on the spool: payload cap and the size of files hashed (lib.sh = capture.py)",
      lambda: f"CK_SPOOL_MAX={capture.SPOOL_MAX} " in _lib and f"CK_HASH_MAX={capture.HASH_MAX} " in _lib)
check("the mod and the shell agree on the spool's path and the error markers (state.py's layout)",
      lambda: all(x in (ROOT / "hooks" / "mod" / "register.tsx").read_text()
                  for x in ("/spool/", "/journal-errors/", "/spool-errors/", "'spool'"))
      and all(x in (ROOT / "src" / "checkpoint" / "state.py").read_text()
              for x in ("spool/<sid>/", "journal-errors/<sid>", "spool-errors/<sid>")))

# --- the journal's format -------------------------------------------------------------------
print("the journal: format, CRC, seq, segments, damage:")
os.environ["CHECKPOINT_SEGMENT_BYTES"] = "2048"
T1 = "t_format"
for i in range(40):
    journal.append(T1, [{"kind": "x", "op": f"op{i}", "session": "s1", "data": {"i": i, "pad": "p" * 40}}])
v = journal.read(T1)
recs = v.records
check("records: v, seq, op, ts, kind, session, task, data, crc; canonical JSON lines",
      lambda: all(set(r) >= {"v", "seq", "op", "ts", "kind", "session", "task", "data", "crc"} for r in recs)
      and all(r["v"] == journal.SCHEMA and r["task"] == T1 for r in recs)
      and all(line == journal.canonical(json.loads(line)) for p in journal.segments(journal.jdir(T1))
              for line in p.read_bytes().splitlines()))
check("seq runs 1..40 without gaps across segments (small segments: several files)",
      lambda: [r["seq"] for r in recs] == list(range(1, 41)) and len(journal.segments(journal.jdir(T1))) > 2 and not v.problems)
check("the journal is private: folder 0700, segments and head 0600",
      lambda: (journal.jdir(T1).stat().st_mode & 0o777) == 0o700
      and all((p.stat().st_mode & 0o777) == 0o600 for p in journal.jdir(T1).iterdir() if p.is_file()))
journal.append(T1, [{"kind": "x", "op": "op7", "session": "s1"}, {"kind": "y", "op": "op7", "session": "s1"}])
check("a second record of the same kind and op is a duplicate and is dropped; another kind for that op is kept",
      lambda: [r["kind"] for r in jrecs(T1)].count("x") == 40 and kinds(T1)[-1] == "y")
last = journal.segments(journal.jdir(T1))[-1]
with open(last, "ab") as f:
    f.write(b'{"v":1,"seq":42,"op":"half')
torn_bytes = last.read_bytes()
v = journal.read(T1)
check("a torn last line: readers stop there, say so (segment, byte, bytes after)",
      lambda: len(v.records) == 41 and v.problems and v.problems[0].kind == "torn" and last.name in v.problems[0].text())
journal.append(T1, [{"kind": "x", "op": "after-torn", "session": "s1"}])
v = journal.read(T1)
check("...the next writer starts a new segment after it: seq continues, everything readable, the torn bytes never rewritten",
      lambda: [r["seq"] for r in v.records] == list(range(1, 43)) and last.read_bytes() == torn_bytes
      and journal.segments(journal.jdir(T1))[-1] != last and [p.kind for p in v.problems] == ["torn"])
seg = journal.segments(journal.jdir(T1))[-1]
journal.append(T1, [{"kind": "x", "op": f"m{i}", "session": "s1"} for i in range(3)])
lines = seg.read_bytes().split(b"\n")
lines[1] = lines[1].replace(b'"m0"', b'"mX"')
_st = seg.stat()
seg.write_bytes(b"\n".join(lines))
os.utime(seg, ns=(_st.st_atime_ns, _st.st_mtime_ns))   # same size, same mtime: as a flipped bit at rest
v = journal.read(T1)
check("a record whose CRC doesn't match (damage at rest): reading that segment stops there and says how much is skipped",
      lambda: any(p.kind == "bad-crc" and "bytes after it not read" in p.text() for p in v.problems)
      and [r["seq"] for r in v.records] == list(range(1, 43)))
journal.append(T1, [{"kind": "x", "op": "after-crc", "session": "s1"}])
v = journal.read(T1)
check("...the next writer notices it though size and mtime are unchanged (the segment's CRC in its head cache) "
      "and continues the seq readers can see, in a new segment",
      lambda: [r["seq"] for r in v.records] == list(range(1, 44)) and v.records[-1]["op"] == "after-crc")
(journal.jdir(T1) / "head.json").unlink()
journal.append(T1, [{"kind": "x", "op": "op3", "session": "s1"}, {"kind": "x", "op": "no-head", "session": "s1"}])
check("without its head cache a writer rebuilds it from the segments (next seq, recent keys: op3 still a duplicate)",
      lambda: [r["op"] for r in jrecs(T1)][-1] == "no-head" and [r["seq"] for r in jrecs(T1)] == list(range(1, 45)))
(journal.jdir(T1) / "head.json").write_text("{not json")
journal.append(T1, [{"kind": "x", "op": "bad-head", "session": "s1"}])
check("...a garbled head cache likewise", lambda: jrecs(T1)[-1]["op"] == "bad-head" and jrecs(T1)[-1]["seq"] == 45)
T2 = "t_v2"
journal.append(T2, [{"kind": "x", "op": "a", "session": "s"}])
with open(journal.segments(journal.jdir(T2))[-1], "ab") as f:
    f.write(journal.encode({"v": 2, "seq": 2, "op": "b", "ts": 1.0, "kind": "x", "session": "s", "task": T2,
                            "data": {}}))
v = journal.read(T2)
check("a record of a newer schema version: the reader stops at it and says so",
      lambda: len(v.records) == 1 and v.problems[0].kind == "schema")
T3 = "t_gap"
for i in range(30):
    journal.append(T3, [{"kind": "x", "op": f"g{i}", "session": "sg", "data": {"pad": "p" * 60}}])
segs = journal.segments(journal.jdir(T3))
segs[1].unlink()   # a whole segment gone
v = journal.read(T3)
check("a missing segment (seq gap at a segment's start): reading stops for good, reported as fatal",
      lambda: v.stopped and v.problems[-1].kind == "seq-gap" and v.problems[-1].fatal
      and v.records[-1]["seq"] < 30)
try:
    journal.append(T3, [{"kind": "x", "op": "behind-gap", "session": "sg"}])
    gap_refused = False
except journal.JournalDamaged:
    gap_refused = True
check("...a writer refuses to append behind it (nobody could read it)", lambda: gap_refused)
state.update_session("sess-gap", task_id=T3, cwd=str(JT))
_t, _w = capture._append("sess-gap", T3, [{"kind": "x", "op": "rescued", "session": "sess-gap"}], sync=False)
check("...capture goes on in a new task that names the damaged one (task.continued), nothing lost",
      lambda: _t != T3 and task_of("sess-gap") == _t and [r["kind"] for r in jrecs(_t)] == ["task.continued", "x"]
      and jrecs(_t)[0]["data"]["from"] == T3)
# Damage at rest in an OLDER segment (a flipped bit; size and mtime kept): every
# record after it is unreadable, so a writer must notice it too, not only in
# the segment it appends to.
T5 = "t_old_flip"
os.environ["CHECKPOINT_SEGMENT_BYTES"] = "600"
for i in range(12):
    journal.append(T5, [{"kind": "x", "op": f"of{i}", "session": "so", "data": {"i": i, "pad": "x" * 60}}])
_segs5 = journal.segments(journal.jdir(T5))
_p5 = _segs5[0]
_st5 = _p5.stat()
_b5 = bytearray(_p5.read_bytes())
_i5 = _b5.index(b'"pad":"x') + 8
_b5[_i5] = ord("y")
_p5.write_bytes(bytes(_b5))
os.utime(_p5, ns=(_st5.st_atime_ns, _st5.st_mtime_ns))
_v5 = journal.read(T5)
try:
    journal.append(T5, [{"kind": "x", "op": "after-old-flip", "session": "so"}], sync=True)
    old_flip_refused = False
except journal.JournalDamaged:
    old_flip_refused = True
except Exception as _e:  # noqa: BLE001  (a failure of the check, not of the run)
    print(f"       ({type(_e).__name__}: {_e})")
    old_flip_refused = False
check("a flipped bit in an older segment (size and mtime kept): readers stop there; a writer notices it too "
      "(every segment's content CRC is in its head cache) and refuses to append behind it",
      lambda: len(_segs5) >= 3 and _v5.stopped and old_flip_refused
      and "after-old-flip" not in [r["op"] for r in journal.read(T5).records])
state.update_session("sess-old-flip", task_id=T5, cwd=str(JT))
try:
    _t5, _w5 = capture._append("sess-old-flip", T5, [{"kind": "x", "op": "rescued5", "session": "sess-old-flip"}],
                               sync=True)
except Exception as _e:  # noqa: BLE001
    print(f"       ({type(_e).__name__}: {_e})")
    _t5 = T5
check("...capture goes on in a new task that names it, and that record is readable there",
      lambda: _t5 != T5 and [r["op"] for r in jrecs(_t5)][-1] == "rescued5" and jrecs(_t5)[0]["data"]["from"] == T5)
# A head cache written by an older version (no CRC for older segments): the
# writer reads those segments once instead of trusting it.
T6 = "t_old_head"
for i in range(8):
    journal.append(T6, [{"kind": "x", "op": f"oh{i}", "session": "so", "data": {"pad": "x" * 60}}])
_h6 = json.loads((journal.jdir(T6) / "head.json").read_text())
check("...every segment's head-cache entry carries its content CRC",
      lambda: len(_h6["segs"]) >= 2 and all(isinstance(c.get("bcrc"), int) for c in _h6["segs"].values()))
for c in _h6["segs"].values():
    c.pop("bcrc", None)
(journal.jdir(T6) / "head.json").write_text(json.dumps(_h6))
_p6 = journal.segments(journal.jdir(T6))[0]
_st6 = _p6.stat()
_b6 = bytearray(_p6.read_bytes())
_b6[_b6.index(b'"pad":"x') + 8] = ord("y")
_p6.write_bytes(bytes(_b6))
os.utime(_p6, ns=(_st6.st_atime_ns, _st6.st_mtime_ns))
try:
    journal.append(T6, [{"kind": "x", "op": "after-old-head", "session": "so"}])
    old_head_refused = False
except journal.JournalDamaged:
    old_head_refused = True
except Exception as _e:  # noqa: BLE001
    print(f"       ({type(_e).__name__}: {_e})")
    old_head_refused = False
check("...and an entry without a CRC (an older head cache) is not trusted: that damage is found as well",
      lambda: old_head_refused)
del os.environ["CHECKPOINT_SEGMENT_BYTES"]

# fsync: on request before returning, and every N records.
print("the journal: fsync, and failures are surfaced:")
_real_durable = journal._durable
_syncs = []
journal._durable = lambda fd, full: (_syncs.append(full), _real_durable(fd, full))[1]
os.environ["CHECKPOINT_FSYNC_EVERY"] = "5"
T4 = "t_fsync"
for i in range(4):
    journal.append(T4, [{"kind": "x", "op": f"f{i}", "session": "s"}])
n_before = len(_syncs)
journal.append(T4, [{"kind": "x", "op": "f4", "session": "s"}])
check("every N records (CHECKPOINT_FSYNC_EVERY=5) the segment is synced, across processes (count kept in head)",
      lambda: n_before == 0 and len(_syncs) == 1)
journal.append(T4, [{"kind": "checkpoint", "op": "c1", "session": "s"}], sync=True)
check("sync=True: synced (F_FULLFSYNC where there is one) before append returns", lambda: _syncs[-1] is True and len(_syncs) == 2)
del os.environ["CHECKPOINT_FSYNC_EVERY"]
journal._durable = _real_durable

# The spool's payloads are the only copy until the journal's records are on
# disk: an unsynced ingest (the mod's timer, a PostToolUse moment) syncs the
# segment before it deletes them, so a power loss can't lose both.
JD = "sess-durable"
JDP = JT / "durable"
JDP.mkdir()
hook("SessionStart", jbase(JD, JDP, source="startup"))
capture.ingest(JD, sync=True)
for i in range(3):
    hook("UserPromptSubmit", jbase(JD, JDP, prompt=f"durable step {i}", prompt_id=f"pd{i}"))
_order = []
_real_unlink = Path.unlink
journal._durable = lambda fd, full: (_order.append(("sync", full)), _real_durable(fd, full))[1]
Path.unlink = lambda self, *a, **kw: (_order.append(("unlink", self.name)), _real_unlink(self, *a, **kw))[1]
try:
    _rd = capture.ingest(JD)     # sync=False, as `run ingest <sid>` from the mod's timer
finally:
    Path.unlink = _real_unlink
    journal._durable = _real_durable
_first_rm = next((i for i, (k, n) in enumerate(_order) if k == "unlink" and n.endswith(".ev")), None)
check("an unsynced ingest syncs the journal (F_FULLFSYNC where there is one) before it deletes the spool payloads",
      lambda: _rd.records >= 3 and _first_rm is not None and ("sync", True) in _order[:_first_rm]
      and not spooled(JD))

# Write failures: ENOSPC and EIO, simulated under the writer; the spool keeps
# the events, the failure is recorded and said once.
JS = "sess-jfail"
JFP = JT / "jfail"
JFP.mkdir()
hook("SessionStart", jbase(JS, JFP, source="startup"))
_real_write = journal.Writer._write
for code in (_errno.ENOSPC, _errno.EIO):
    hook("UserPromptSubmit", jbase(JS, JFP, prompt=f"step under {_errno.errorcode[code]}", prompt_id=f"pf{code}"))
    waiting = len(spooled(JS))

    def _failing(self, data, _code=code):
        raise OSError(_code, os.strerror(_code))

    journal.Writer._write = _failing
    try:
        r1 = capture.ingest(JS)
        r2 = capture.ingest(JS)
        status_then = "\n".join(core.journal_status(JS))
    finally:
        journal.Writer._write = _real_write
    check(f"{_errno.errorcode[code]} while writing: ingest reports it, the events stay in the spool, said once",
          lambda: r1.error and _errno.errorcode[code] in (journal.failure(JS) or {}).get("errno", "")
          and len(spooled(JS)) == waiting and r1.new_failure and not r2.new_failure)
    check(f"...{_errno.errorcode[code]}: /check:status says RECORDING FAILED", lambda: "RECORDING FAILED" in status_then)
    doc = subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "doctor", JS], capture_output=True, text=True)
    check(f"...{_errno.errorcode[code]}: env.sh doctor names it", lambda: "journal: WRITING FAILED" in doc.stdout)
    r3 = capture.ingest(JS)
    check(f"...{_errno.errorcode[code]}: once writes work again the spool drains, nothing lost, the failure is history",
          lambda: not r3.error and not spooled(JS) and journal.failure(JS) is None and journal.last_failure(JS)
          and any(r["kind"] == "prompt" and r["data"]["text"] == f"step under {_errno.errorcode[code]}"
                  for r in jrecs(task_of(JS))))

# The same through a hook: the first Python moment after a failure says so.
seg_ro = journal.segments(journal.jdir(task_of(JS)))[-1]
hook("UserPromptSubmit", jbase(JS, JFP, prompt="more", prompt_id="pmore"))
seg_ro.chmod(0o400)
out = hook("UserPromptSubmit", jbase(JS, JFP, prompt="/check:status", prompt_id="pstat"))
seg_ro.chmod(0o600)
check("a journal that can't be written (EACCES): the hook's output says recording failed, nothing is lost yet",
      lambda: "recording failed" in (out or {}).get("systemMessage", "") and "wait in the spool" in (out or {}).get("systemMessage", ""))
out = hook("UserPromptSubmit", jbase(JS, JFP, prompt="/check:status", prompt_id="pstat2"))
check("...and the next Python moment writes it all once it works", lambda: journal.failure(JS) is None and not spooled(JS)
      and {"more", "/check:status"} <= {r["data"]["text"] for r in jrecs(task_of(JS)) if r["kind"] == "prompt"})

# A real OS-level write failure: the file size limit (EFBIG), as a full disk would.
JS2 = "sess-fsize"
hook("SessionStart", jbase(JS2, JFP, source="startup"))
for i in range(5):
    hook("UserPromptSubmit", jbase(JS2, JFP, prompt="x" * 3000 + str(i), prompt_id=f"fz{i}"))


def _limit():
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    seg_size = max(p.stat().st_size for p in journal.segments(journal.jdir(task_of(JS2))))
    _resource.setrlimit(_resource.RLIMIT_FSIZE, (seg_size + 100, seg_size + 100))


r = subprocess.run([sys.executable, str(ROOT / "run.py"), "ingest", JS2], capture_output=True, text=True,
                   preexec_fn=_limit, timeout=60)
check("a real write error (RLIMIT_FSIZE: EFBIG, like a full disk): ingest exits 1 and says so, the failure is recorded",
      lambda: r.returncode == 1 and "error" in r.stdout and journal.failure(JS2) is not None
      and journal.failure(JS2)["errno"] == "EFBIG")
v = journal.read(task_of(JS2))
check("...the journal is left readable (at worst a torn tail the next writer steps over)",
      lambda: all(p.kind == "torn" for p in v.problems))
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "ingest", JS2], capture_output=True, text=True, timeout=60)
check("...without the limit it all goes in: five prompts once each", lambda: r.returncode == 0
      and sorted(r_["data"]["prompt_id"] for r_ in jrecs(task_of(JS2)) if r_["kind"] == "prompt")
      == [f"fz{i}" for i in range(5)])

# A spool that can't be written: the hook says so (once), doctor and the status line see it.
JS3 = "sess-spoolfail"
hook("SessionStart", jbase(JS3, JFP, source="startup"))
capture.spool_dir(JS3).chmod(0o500)
o1 = hook("PreToolUse", jbase(JS3, JFP, tool_name="Read", tool_use_id="sf1", tool_input={"file_path": "x"}))
o2 = hook("PreToolUse", jbase(JS3, JFP, tool_name="Read", tool_use_id="sf2", tool_input={"file_path": "x"}))
doc = subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "doctor", JS3], capture_output=True, text=True)
capture.spool_dir(JS3).chmod(0o700)
check("a spool that can't be written: the hook says the record is lost (systemMessage), once, not silently",
      lambda: "couldn't record this event" in (o1 or {}).get("systemMessage", "") and o2 is None
      and state.p("spool-errors", JS3).exists())
check("...env.sh doctor reports it (spool: RECORDING FAILED)", lambda: "spool: RECORDING FAILED" in doc.stdout)
hook("PreToolUse", jbase(JS3, JFP, tool_name="Read", tool_use_id="sf3", tool_input={"file_path": "x"}))
check("...a later record that works clears the mark", lambda: not state.p("spool-errors", JS3).exists())
# ...but the failure stays on record (acceptance: after a full-disk window
# nothing was left for /check:doctor): its history, and a journal record of
# the window whose events are lost.
_last3 = state.p("spool-errors", JS3 + ".last")
check("...it becomes history: spool-errors/<session>.last says since when, why, and when recording worked again",
      lambda: "can't write to" in _last3.read_text() and "recording worked again at" in _last3.read_text())
capture.ingest(JS3)
_gap3 = [r for r in jrecs(task_of(JS3)) if r["kind"] == "recording.gap"]
check("...the journal records the gap once (recording.gap: from when, until when), sanitized",
      lambda: len(_gap3) == 1 and "can't write to" in _gap3[0]["data"]["failed"] and _gap3[0]["data"]["recovered"])
check("...the summary's header names it: that window is missing, never filled in",
      lambda: "Recording gap: the hooks couldn't record events from" in summary.build(task_of(JS3)).text.split("\n## ")[0])
doc = subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "doctor", JS3], capture_output=True, text=True)
check("...env.sh doctor (/check:doctor) names the recovered failure and that its events are lost",
      lambda: "spool: last recording failure (recovered; its events are lost)" in doc.stdout
      and "RECORDING FAILED" not in doc.stdout)
check("...so does /check:status", lambda: "Recording failed earlier in the hooks" in "\n".join(core.journal_status(JS3)))
check("...and the reserve is armed again", lambda: state.reserve_path("spool-errors", JS3).exists())

# A full disk: no new file can be written, not even the marker. The reserve
# armed at session start becomes it (a rename and an overwrite in place; the
# TUI acceptance fills a real disk image).
JS4 = "sess-diskfull"
hook("SessionStart", jbase(JS4, JFP, source="startup"))
check("SessionStart arms a reserve for each failure marker (spool-errors/, journal-errors/; RESERVE_BYTES)",
      lambda: all(state.reserve_path(d, JS4).stat().st_size == state.RESERVE_BYTES
                  for d in ("spool-errors", "journal-errors")))
_df = subprocess.run([SH, "-c", 'st=$1; root=$2; . "$root/hooks/lib.sh"; ck_write() { return 1; }; input=x; '
                      'ck_spool_failed "$3" "cannot write to x (disk full?)"; printf "%s" "$CK_SPOOL_SAY"; '
                      'CK_SPOOL_SAY=; ck_spool_failed "$3" "again"; printf "|%s" "$CK_SPOOL_SAY"',
                      "sh", os.environ["CHECKPOINT_HOME"], str(ROOT), JS4], capture_output=True, text=True, timeout=30)
_m4 = state.p("spool-errors", JS4)
check("...with no new file writable the reserve becomes spool-errors/<session>: its first line the time and why, "
      "said once (the next failure is silent: on record already)",
      lambda: _m4.exists() and _m4.read_text().splitlines()[0].endswith(" cannot write to x (disk full?)")
      and not state.reserve_path("spool-errors", JS4).exists()
      and _df.stdout.startswith("checkpoint: couldn't record this event") and _df.stdout.endswith("|"))
_real_wj = state.write_json


def _wj_enospc(path, data):
    raise OSError(_errno.ENOSPC, "No space left on device")


state.write_json = _wj_enospc
try:
    _ok4 = journal.record_failure(JS4, "t_full", OSError(_errno.ENOSPC, "No space left on device"))
finally:
    state.write_json = _real_wj
check("...Python's journal failure the same way: journal-errors/<session> from its reserve, readable JSON",
      lambda: _ok4 and (journal.failure(JS4) or {}).get("errno") == "ENOSPC"
      and not state.reserve_path("journal-errors", JS4).exists())
journal.clear_failure(JS4)
check("...once writes work: history (.last), and its reserve armed again",
      lambda: (journal.last_failure(JS4) or {}).get("errno") == "ENOSPC" and journal.failure(JS4) is None
      and state.reserve_path("journal-errors", JS4).exists())

# --- capture ------------------------------------------------------------------------------
print("capture: the spool, without Python on the common path:")
CP = JT / "capture proj ü"      # a space and a non-ASCII letter
CP.mkdir()
jgit(CP, "init", "-q")
(CP / "app.py").write_text("print('v1')\n")
jgit(CP, "add", "app.py")
jgit(CP, "commit", "-qm", "v1")
CL = fake_claude()
EC = {"CLAUDE_PID": str(CL.pid), **HENV}
SC = "sess-capture-1"
TPC = JT / "tc.jsonl"
TPC.write_text("")
out = hook("SessionStart", jbase(SC, CP, TPC, source="startup", model="m"), EC)
TC = task_of(SC)
check("SessionStart: a new task, its session.start (lineage) and repo identity (root, branch, HEAD) in the journal",
      lambda: TC.startswith("t_") and kinds(TC)[:2] == ["session.start", "repo"]
      and jrecs(TC)[1]["data"]["root"] == os.path.realpath(CP) and len(jrecs(TC)[1]["data"]["head"]) == 40)
n0 = pycalls()
hook("UserPromptSubmit", jbase(SC, CP, TPC, prompt="Build the widget and keep the API stable", prompt_id="g1"), EC)
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Read", tool_use_id="rd1", tool_input={"file_path": str(CP / "app.py")}), EC)
hook("PostToolUse", jbase(SC, CP, TPC, tool_name="Read", tool_use_id="rd1", tool_input={"file_path": "x"},
                          tool_response={"type": "text"}), EC)
for ev, extra in (("SubagentStart", {"agent_id": "ag1", "agent_type": "general-purpose"}),
                  ("Notification", {"message": "Claude needs your permission to use Bash", "notification_type": "permission_prompt"}),
                  ("TaskCreated", {"task_id": "1", "task_subject": "Write widget"}),
                  ("PermissionDenied", {"tool_name": "Bash", "tool_use_id": "den1", "tool_input": {"command": "rm -rf /"}})):
    hook(ev, jbase(SC, CP, TPC, **extra), EC)
check("prompts, tool calls, subagents, notifications, task and permission events: spooled, no Python started",
      lambda: pycalls() == n0 and len(spooled(SC)) == 7 and len(index_lines(SC)) == 8)
check("...each spool record: a payload file of its own and one index line naming it (event, file, bytes)",
      lambda: all(len(ln.split("\t")) >= 4 and ln.split("\t")[0] == "1" for ln in index_lines(SC))
      and [ln.split("\t")[1] for ln in index_lines(SC)][:4] == ["SessionStart", "UserPromptSubmit", "PreToolUse",
                                                                "PostToolUse"]
      and all((capture.spool_dir(SC) / ln.split("\t")[2]).exists() for ln in index_lines(SC)[1:]))
check("...private: the spool folder 0700, its files 0600",
      lambda: (capture.spool_dir(SC).stat().st_mode & 0o777) == 0o700
      and all((p.stat().st_mode & 0o777) == 0o600 for p in capture.spool_dir(SC).iterdir()))
r = capture.ingest(SC)
check("ingest: every event becomes its record (prompt, intent, outcome, subagent, notification, plan event, denial)",
      lambda: r.events == 7 and {"prompt", "tool.intent", "tool.outcome", "subagent.start", "notification", "plan.event"}
      <= set(kinds(TC)) and any(x["kind"] == "tool.outcome" and x["data"].get("denied") == "permission"
                                for x in jrecs(TC)))
check("...then its payload files are deleted and the cursor moved: ingesting again adds nothing",
      lambda: not spooled(SC) and capture.ingest(SC).records == 0 and kinds(TC).count("prompt") == 1)

# Edits: before/after hashes from the shell (sha256 of the file then). The edit
# strings are random: a short one ("v1") can turn up in the temp path.
before = _hashlib.sha256((CP / "app.py").read_bytes()).hexdigest()
_ED_STR = {"old_string": "old-" + _rand(16), "new_string": "new-" + _rand(16)}
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Edit", tool_use_id="ed1",
                         tool_input={"file_path": str(CP / "app.py"), **_ED_STR}), EC)
(CP / "app.py").write_text("print('v2')\n")
after = _hashlib.sha256((CP / "app.py").read_bytes()).hexdigest()
hook("PostToolUse", jbase(SC, CP, TPC, tool_name="Edit", tool_use_id="ed1",
                          tool_input={"file_path": str(CP / "app.py"), **_ED_STR},
                          tool_response={"filePath": str(CP / "app.py"), "userModified": False}), EC)
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Write", tool_use_id="wr1",
                         tool_input={"file_path": str(CP / "new.py"), "content": "x = 1\n"}), EC)
(CP / "new.py").write_text("x = 1\n")
hook("PostToolUse", jbase(SC, CP, TPC, tool_name="Write", tool_use_id="wr1",
                          tool_input={"file_path": str(CP / "new.py"), "content": "x = 1\n"},
                          tool_response={"type": "create"}), EC)
capture.ingest(SC)
ed = {(x["kind"], x["op"]): x["data"] for x in jrecs(TC) if x["op"] in ("ed1", "wr1")}
check("Edit: the before hash (PreToolUse) and after hash (PostToolUse) are the file's sha256 then; no edit strings kept",
      lambda: ed[("tool.intent", "ed1")]["before"] == before and ed[("tool.outcome", "ed1")]["after"] == after
      and not any(s in json.dumps(ed[(k, "ed1")]) for s in _ED_STR.values() for k in ("tool.intent", "tool.outcome")))
check("Write of a new file: before 'absent', after its hash, the content's size and hash (not the content)",
      lambda: ed[("tool.intent", "wr1")]["before"] == "absent"
      and ed[("tool.outcome", "wr1")]["after"] == _hashlib.sha256(b"x = 1\n").hexdigest()
      and ed[("tool.intent", "wr1")]["input"]["content_bytes"] == 6)

# Validation: a test-like Bash command pins the version it runs against.
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="vt1",
                         tool_input={"command": "python3 -m pytest -q tests/", "description": "tests"}), EC)
hook("PostToolUseFailure", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="vt1",
                                 tool_input={"command": "python3 -m pytest -q tests/"},
                                 error="Exit code 1\n1 failed, 3 passed", is_interrupt=False), EC)
capture.ingest(SC)
vv = [x for x in jrecs(TC) if x["op"] == "vt1"]
ver = next((x["data"] for x in vv if x["kind"] == "validation.version"), {})
check("a test command: its intent is marked validation, a background snapshot records HEAD, branch and the dirty "
      "files' hashes at its start, its outcome the exit code",
      lambda: any(x["kind"] == "tool.intent" and x["data"].get("validation") == "test" for x in vv)
      and ver.get("head") and {os.path.realpath(k): v for k, v in ver.get("files", {}).items()}.get(
          os.path.realpath(CP / "app.py")) == after
      and any(x["kind"] == "tool.outcome" and x["data"].get("exit") == 1 for x in vv))
# A failed command that isn't a test, build or check: Claude Code's error is
# "Exit code N" and then the output so far (a `cat` of a file, say).
_fail_pw = _rand(18)
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="nf1",
                         tool_input={"command": "cat deploy/notes.txt && ./deploy.sh"}), EC)
hook("PostToolUseFailure", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="nf1",
                                 tool_input={"command": "cat deploy/notes.txt && ./deploy.sh"},
                                 error=f"Exit code 1\nadmin login: alice / {_fail_pw}\ndeploy failed",
                                 is_interrupt=False), EC)
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="nf2", tool_input={"command": "./sync.sh"}), EC)
hook("PostToolUseFailure", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="nf2", tool_input={"command": "./sync.sh"},
                                 error=f"Interrupted by user\ncustomer row {_fail_pw}", is_interrupt=True), EC)
capture.ingest(SC)
nf = {x["op"]: x["data"] for x in jrecs(TC) if x["op"] in ("nf1", "nf2", "vt1") and x["kind"] == "tool.outcome"}
check("a failed command that isn't a test, build or check: only Claude Code's first line (its exit code, an "
      "interrupt) and the size are journaled, never the output",
      lambda: nf["nf1"].get("error") == "Exit code 1" and nf["nf1"].get("exit") == 1 and nf["nf1"]["error_chars"] > 30
      and nf["nf2"].get("error") == "Interrupted by user" and nf["nf2"].get("interrupted") is True
      and not scan_for([_fail_pw], journal.jdir(TC)))
check("...a failed test keeps its scrubbed output tail (the evidence)",
      lambda: nf["vt1"].get("error") == "Exit code 1\n1 failed, 3 passed")
check("validation_kind: tests, builds and checks; not ordinary commands",
      lambda: [capture.validation_kind(c) for c in ("pytest -q", "npm run build", "cargo clippy", "make", "./run_tests.sh",
                                            "go test ./...", "ls -la", "git checkout main", "echo hi")]
      == ["test", "build", "check", "build", "test", "test", "", "", ""])

# Duplicate deliveries and out-of-order observations.
dup = jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="dup1", tool_input={"command": "echo once"})
hook("PreToolUse", dup, EC)
hook("PreToolUse", dup, EC)
hook("PostToolUse", dict(dup, tool_response={"stdout": "once", "stderr": "", "interrupted": False}), EC)
hook("PostToolUse", dict(dup, tool_response={"stdout": "once", "stderr": "", "interrupted": False}), EC)
capture.ingest(SC)
check("the same tool_use_id delivered twice (Pre and Post each): one intent, one outcome",
      lambda: [(x["kind"]) for x in jrecs(TC) if x["op"] == "dup1"] == ["tool.intent", "tool.outcome"])
ooo = jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="ooo1", tool_input={"command": "echo late-pre"})
hook("PostToolUse", dict(ooo, tool_response={"stdout": "x", "stderr": "", "interrupted": False}), EC)
capture.ingest(SC)
hook("PreToolUse", ooo, EC)
capture.ingest(SC)
sm = summary.build(TC)
check("PostToolUse before its PreToolUse (out of order): one op, done, never 'in flight'",
      lambda: [(x["kind"]) for x in jrecs(TC) if x["op"] == "ooo1"] == ["tool.outcome", "tool.intent"]
      and "late-pre" not in "\n".join(sm.data["in_flight"]) and "echo late-pre`: ok (observed" in sm.text)

# Many hooks of one session at once, with payloads over 1 KiB: the index stays whole.
big = "y" * 3000
procs_ = [subprocess.Popen([SH, str(ROOT / "hooks" / "fast.sh"), "PreToolUse"], stdin=subprocess.PIPE,
                           stdout=subprocess.DEVNULL, env=dict(os.environ, **EC))
          for _ in range(24)]
for i, p_ in enumerate(procs_):
    p_.stdin.write(json.dumps(jbase(SC, CP, TPC, tool_name="Grep", tool_use_id=f"par{i}",
                                    tool_input={"pattern": big + str(i)})).encode())
    p_.stdin.close()
for p_ in procs_:
    p_.wait(timeout=60)
lines_ok = all(len(ln.split("\t")) >= 4 and ln.split("\t")[2].endswith(".ev") for ln in index_lines(SC))
capture.ingest(SC)
check("24 hooks of one session at once (payloads over 1 KiB): index lines whole, all 24 intents journaled",
      lambda: lines_ok and len({x["op"] for x in jrecs(TC) if x["op"].startswith("par")}) == 24)

# The spool's appends themselves: 16 writers at once, 40 payloads of ~3 KiB
# each (one write() can't hold them): every index line whole, every payload
# file whole JSON.
SA_ = "sess-atomic"
_drv = JT / "spool-writer.sh"
_drv.write_text(f""". '{ROOT}/hooks/lib.sh'
st=$1
i=0
while [ "$i" -lt 40 ]; do
    input=$(printf '{{"session_id":"{SA_}","tool_name":"Grep","tool_use_id":"w%s-%s","p":"%s"}}' "$2" "$i" "$4")
    ck_spool {SA_} PreToolUse
    i=$((i + 1))
done
""")
_pad = "q" * 3000
_ws = [subprocess.Popen([SH, str(_drv), str(state.home()), str(k), "-", _pad]) for k in range(16)]
for _w in _ws:
    _w.wait(timeout=120)
_idx = index_lines(SA_)
_files_ok = True
for _ln in _idx:
    try:
        _pl = json.loads((capture.spool_dir(SA_) / _ln.split("\t")[2]).read_text())
        _files_ok = _files_ok and _pl["p"] == _pad
    except (OSError, ValueError, IndexError, KeyError):
        _files_ok = False
check("16 shell writers at once, 40 payloads of ~3 KiB each: 640 index lines, each whole, each naming a whole payload",
      lambda: len(_idx) == 640 and all(len(_l.split("\t")) == 4 and _l.split("\t")[2].endswith(".ev") for _l in _idx)
      and len({_l.split("\t")[2] for _l in _idx}) == 640 and _files_ok)
capture.ingest(SA_)

# An oversized payload is cut, and still says what it was.
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Write", tool_use_id="huge1",
                         tool_input={"file_path": str(CP / "huge.txt"), "content": "z" * 400000}), EC)
cut_line = [ln for ln in index_lines(SC) if "cut=" in ln]
sizes = [p.stat().st_size for p in capture.spool_dir(SC).glob("*.ev")]
capture.ingest(SC)
hu = next(x for x in jrecs(TC) if x["op"] == "huge1")
check("a payload over the cap (256 KiB): cut in the spool (cut=<bytes>), its record keeps tool, id and path",
      lambda: cut_line and max(sizes) <= capture.SPOOL_MAX and hu["data"]["tool"] == "Write"
      and hu["data"]["path"] == str(CP / "huge.txt") and hu["data"]["payload_cut"] > 400000)

# An orphan payload (its hook died before the index line): taken after a minute, not before.
orph = capture.spool_dir(SC) / "99999.ev"
orph.write_text(json.dumps(jbase(SC, CP, TPC, hook_event_name="PreToolUse", tool_name="Bash", tool_use_id="orph1",
                                 tool_input={"command": "echo orphan"})) + "\n")
capture.ingest(SC)
fresh_left = orph.exists()
os.utime(orph, (time.time() - 120,) * 2)
capture.ingest(SC)
check("a payload whose index line never came: left alone while fresh, ingested after a minute (marked orphan)",
      lambda: fresh_left and not orph.exists()
      and any(x["op"] == "orph1" and x["data"].get("orphan") for x in jrecs(TC)))

# A crash mid-ingest (kill -9) and ingest again: every event once.
SK = "sess-killingest"
hook("SessionStart", jbase(SK, CP, TPC, source="startup"), EC)
capture.ingest(SK)
with open(capture.spool_dir(SK) / "index", "a") as idx:
    for i in range(400):
        txt_ = json.dumps(jbase(SK, CP, TPC, tool_name="Read", tool_use_id=f"k{i}", tool_input={"file_path": f"f{i}"}))
        (capture.spool_dir(SK) / f"k{i}.ev").write_text(txt_ + "\n")
        idx.write(f"1\tPreToolUse\tk{i}.ev\t{len(txt_)}\n")
p_ = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "ingest", SK], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
time.sleep(0.15)
p_.kill()
p_.wait()
capture.ingest(SK)
ops_ = [x["op"] for x in jrecs(task_of(SK)) if x["op"].startswith("k")]
check("ingest killed with SIGKILL mid-way, then run again: all 400 events journaled exactly once, the spool empty",
      lambda: sorted(ops_) == sorted(f"k{i}" for i in range(400)) and not spooled(SK)
      and all(p.kind == "torn" for p in journal.read(task_of(SK)).problems))

# Two sessions of one task writing at once (a forked session shares the task).
TT = "t_two_writers"


def _writer(tag, n):
    return subprocess.Popen([sys.executable, "-c", (
        "import sys; sys.path.insert(0, sys.argv[1]); from checkpoint import journal\n"
        "for i in range(int(sys.argv[4])):\n"
        "    journal.append(sys.argv[2], [{'kind': 'x', 'op': f'{sys.argv[3]}{i}', 'session': sys.argv[3],"
        " 'data': {'i': i}}])\n"), str(ROOT / "src"), TT, tag, str(n)])


ws = [_writer("A", 300), _writer("B", 300)]
for w_ in ws:
    w_.wait(timeout=120)
v = journal.read(TT)
check("two writers (two sessions of one task) at once: 600 records, seq 1..600 without a gap, every CRC good",
      lambda: [r["seq"] for r in v.records] == list(range(1, 601)) and not v.problems
      and {r["session"] for r in v.records} == {"A", "B"})

# --- the summary -------------------------------------------------------------------------
print("the recovery summary (deterministic, from the journal):")
SM = "sess-summary"
SMP = JT / "summary proj"
SMP.mkdir()
jgit(SMP, "init", "-q")
(SMP / "lib.py").write_text("A = 1\n")
jgit(SMP, "add", "lib.py")
jgit(SMP, "commit", "-qm", "init")
CLS = fake_claude()
ES = {"CLAUDE_PID": str(CLS.pid), **HENV}
TPS = JT / "tsum.jsonl"
TPS.write_text("")
hook("SessionStart", jbase(SM, SMP, TPS, source="startup"), ES)
hook("UserPromptSubmit", jbase(SM, SMP, TPS, prompt=f"Ship v2 of lib. Use token {S['token']} nowhere.",
                               prompt_id="s1"), ES)
hook("UserPromptSubmit", jbase(SM, SMP, TPS, prompt="<task-notification>agent done</task-notification>",
                               prompt_id="s2"), ES)
hook("UserPromptSubmit", jbase(SM, SMP, TPS, prompt="/check:status", prompt_id="s3"), ES)
hook("UserPromptSubmit", jbase(SM, SMP, TPS, prompt="Also add a changelog entry", prompt_id="s4"), ES)
mcp_env = dict(os.environ, CLAUDE_PID=str(CLS.pid), CLAUDE_CODE_SESSION_ID=SM)
resp = mcp([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "checkpoint_note", "arguments": {
                "kind": "constraint", "text": f"Never touch prod; never print GH_TOKEN={S['github classic']}"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "checkpoint_note", "arguments": {
                "kind": "decision", "text": "Keep the v1 API"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "checkpoint_note", "arguments": {
                "kind": "nonsense", "text": "x"}}}], mcp_env)
note_res = json.loads(resp[2]["result"]["content"][0]["text"])
check("checkpoint_note (MCP): recorded in the session's task, synced, with its seq; a bad kind refused",
      lambda: note_res.get("ok") and note_res.get("task") == task_of(SM) and note_res.get("seq", 0) > 0
      and resp[4]["result"]["isError"])
for tid, tool, inp, resp_ in (
        ("tc1", "TaskCreate", {"subject": "Write lib v2", "description": "d"}, {"task": {"id": "1", "subject": "Write lib v2"}}),
        ("tc2", "TaskCreate", {"subject": "Changelog"}, {"task": {"id": "2", "subject": "Changelog"}}),
        ("tu1", "TaskUpdate", {"taskId": "1", "status": "in_progress"}, {"success": True, "taskId": "1",
                                                                        "statusChange": {"from": "pending", "to": "in_progress"}}),
        ("tu2", "TaskUpdate", {"taskId": "1", "status": "completed"}, {"success": True, "taskId": "1",
                                                                      "statusChange": {"from": "in_progress", "to": "completed"}})):
    hook("PreToolUse", jbase(SM, SMP, TPS, tool_name=tool, tool_use_id=tid, tool_input=inp), ES)
    hook("PostToolUse", jbase(SM, SMP, TPS, tool_name=tool, tool_use_id=tid, tool_input=inp, tool_response=resp_), ES)
hook("PreToolUse", jbase(SM, SMP, TPS, tool_name="TaskUpdate", tool_use_id="tu3",
                         tool_input={"taskId": "2", "status": "in_progress"}), ES)
hook("PreToolUse", jbase(SM, SMP, TPS, tool_name="Edit", tool_use_id="se1",
                         tool_input={"file_path": str(SMP / "lib.py"), "old_string": "1", "new_string": "2"}), ES)
(SMP / "lib.py").write_text("A = 2\n")
hook("PostToolUse", jbase(SM, SMP, TPS, tool_name="Edit", tool_use_id="se1",
                          tool_input={"file_path": str(SMP / "lib.py"), "old_string": "1", "new_string": "2"},
                          tool_response={"userModified": False}), ES)
hook("PreToolUse", jbase(SM, SMP, TPS, tool_name="Bash", tool_use_id="st1",
                         tool_input={"command": "pytest -q", "description": "run tests"}), ES)
hook("PostToolUse", jbase(SM, SMP, TPS, tool_name="Bash", tool_use_id="st1", tool_input={"command": "pytest -q"},
                          tool_response={"stdout": "4 passed", "stderr": "", "interrupted": False}), ES)
hook("SubagentStart", jbase(SM, SMP, TPS, agent_id="sub1", agent_type="Explore"), ES)
hook("PreToolUse", jbase(SM, SMP, TPS, agent_id="sub1", tool_name="Bash", tool_use_id="sb1",
                         tool_input={"command": "grep -r v2 ."}), ES)
hook("PreToolUse", jbase(SM, SMP, TPS, tool_name="Bash", tool_use_id="fly1",
                         tool_input={"command": "scp build.tgz deploy@staging:/srv/"}), ES)
capture.ingest(SM)   # as every caller does first (status, resume, save, the CLI)
s1 = summary.build(task_of(SM))
txt = s1.text
dump("s1", txt)
dump("s1-again", summary.build(task_of(SM)).text)
check("Goal: the task's first user prompt (scrubbed); notifications and /check:* commands are not instructions",
      lambda: "## Goal\nShip v2 of lib." in txt and S["token"] not in txt and s1.data["instructions"] == ["Also add a changelog entry"])
check("Constraints & decisions: the checkpoint_note records (scrubbed), labelled observed",
      lambda: "constraint: Never touch prod" in txt and "decision: Keep the v1 API" in txt and S["github classic"] not in txt
      and "observed: checkpoint_note" in txt)
check("Plan: steps from TaskCreate/TaskUpdate with their recorded status; an update with no result is 'outcome unknown'",
      lambda: "- [completed] Write lib v2 (#1; observed: TaskUpdate" in txt and "- [pending] Changelog (#2" in txt
      and "change to in_progress requested" in txt and "outcome unknown" in txt
      and "Progress (inferred from the steps' recorded statuses): 1 of 2 completed." in txt)
check("Files changed: before/after hashes and whether the file still matches",
      lambda: f"before {_hashlib.sha256(b'A = 1' + bytes([10])).hexdigest()[:12]}" in txt
      and f"after {_hashlib.sha256(b'A = 2' + bytes([10])).hexdigest()[:12]}" in txt
      and "unchanged since (current hash matches)" in txt)
check("Validation: the result (no exit code claimed: the result states none), the HEAD the hook read before it ran, "
      "the snapshot's lag, and that it is still current",
      lambda: "test `pytest -q`: passed (no error reported)" in txt and "exit 0" not in section(txt, "Validation")
      and "started on HEAD " in txt and "(read by the hook before the command ran: observed)" in txt
      and re.search(r"taken ~[0-9]+ ms after it started", txt) and "still current" in txt
      and "observed at its start" not in txt)
check("In flight / unknown: intents without outcome, never in Done; a running session says 'no result yet'",
      lambda: "scp build.tgz deploy@staging:/srv/" in "\n".join(s1.data["in_flight"]) and "no result yet" in txt
      and "scp build.tgz" not in section(txt, "Done")
      and "check whether it already took effect" in txt)
check("Children: the subagent: started, no stop recorded, so its result not observed",
      lambda: re.search(r"subagent sub1 \(Explore\): started \(observed, seq [0-9]+\); no stop recorded, so its result "
                        r"was not observed", txt))
check("Lineage and Evidence: the session, the journal path and seq range", lambda: f"session {SM} (startup)" in txt
      and str(journal.jdir(task_of(SM))) in txt)
check("deterministic: built twice from the same journal and files, the same text (but the build time)",
      lambda: re.sub(r"built [0-9: -]+\)", "", txt) == re.sub(r"built [0-9: -]+\)", "", summary.build(task_of(SM)).text))
_short_s = summary.build(task_of(SM), limit=900).text
check("bounded: under a limit the least important sections are left out and named (goal first, evidence last)",
      lambda: len(_short_s) <= 900 and "left out to stay under 900 characters" in _short_s and "## Goal" in _short_s
      and "## Evidence" not in _short_s)
(SMP / "lib.py").write_text("A = 3\n")
capture.ingest(SM)
s2 = summary.build(task_of(SM))
check("after a later change to a file it ran against: the validation is STALE (inferred), the file CHANGED since",
      lambda: "STALE (inferred:" in s2.text and "lib.py changed" in s2.text and "CHANGED since its last recorded edit" in s2.text)
jgit(SMP, "commit", "-qam", "v3")
s3 = summary.build(task_of(SM))
check("...and a moved HEAD says so", lambda: "HEAD moved" in s3.text)
CLS.kill()
CLS.wait()
capture.ingest(SM)
s4 = summary.build(task_of(SM))
check("the session's process gone (killed): in-flight intents read 'outcome unknown', never done; lineage says so",
      lambda: "outcome unknown: its session ended or its process is gone" in s4.text
      and "no end recorded: killed or crashed (process gone)" in s4.text)
hook("UserPromptSubmit", jbase(SM, SMP, TPS, prompt="/check:pause", prompt_id="pp1"), ES)
hook("PreToolUse", jbase(SM, SMP, TPS, tool_name="Bash", tool_use_id="denied1", tool_input={"command": "make deploy"}), ES)
capture.ingest(SM)
s5 = summary.build(task_of(SM))
check("pause recorded (observed), and a call denied while paused is neither in flight nor a failure",
      lambda: "Paused at" in s5.text and "make deploy" not in "\n".join(s5.data["in_flight"])
      and "make deploy" not in "\n".join(s5.data["failures"])
      and any(r["kind"] == "pause" for r in jrecs(task_of(SM))))
hook("UserPromptSubmit", jbase(SM, SMP, TPS, prompt="go on", prompt_id="pp2"), ES)
check("...and the resume", lambda: "resume" in [r["kind"] for r in jrecs(task_of(SM))][-4:]
      and any(r["kind"] == "resume" for r in jrecs(task_of(SM))))

# --- what an outcome says: background work, exit codes, failed edits ------------------------
print("outcomes: a start is not a result, exit codes only when stated, failed edits, staleness, versions:")
BG = "sess-background"
BGP = JT / "bg proj"
BGP.mkdir()
jgit(BGP, "init", "-q")
(BGP / "m.py").write_text("M = 1\n")
jgit(BGP, "add", "m.py")
jgit(BGP, "commit", "-qm", "m")
CLB = fake_claude()
EB = {"CLAUDE_PID": str(CLB.pid), **HENV}
TPBG = JT / "tbg.jsonl"
TPBG.write_text("")
hook("SessionStart", jbase(BG, BGP, TPBG, source="startup"), EB)
hook("UserPromptSubmit", jbase(BG, BGP, TPBG, prompt="Run the slow tests in the background", prompt_id="bg0"), EB)
# 1. run_in_background: the result only says it started (a backgroundTaskId).
_bgi = {"command": "sleep 40; pytest -q; echo ckbg-marker", "run_in_background": True}
hook("PreToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id="bg1", tool_input=_bgi), EB)
hook("PostToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id="bg1", tool_input=_bgi,
                          tool_response={"stdout": "", "stderr": "", "interrupted": False, "isImage": False,
                                         "backgroundTaskId": "b4pv3di4t"}), EB)
# 2. A foreground command moved to the background so a message could reach the
# model (2.1.301); 3. one the user moved with Ctrl+B.
for tid, cmd, flag in (("bg2", "python3 -m pytest -q tests/slow", "backgroundedToDeliverMessage"),
                       ("bg3", "make release-build", "backgroundedByUser")):
    hook("PreToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id=tid, tool_input={"command": cmd}), EB)
    hook("PostToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id=tid, tool_input={"command": cmd},
                              tool_response={"stdout": "", "stderr": "", "interrupted": False,
                                             "backgroundTaskId": f"id-{tid}", flag: True}), EB)
# 4. An async subagent (Agent always runs in the background on 2.1.301), one
# with a start and no stop, one with a stop, one no hook saw at all.
for tid, aid, desc in (("ag1", "agentAAA111", "Review the docs"), ("ag2", "agentBBB222", "Lint the repo"),
                       ("ag3", "agentCCC333", "Count the files")):
    _ai = {"description": desc, "prompt": "do it", "subagent_type": "general-purpose"}
    hook("PreToolUse", jbase(BG, BGP, TPBG, tool_name="Agent", tool_use_id=tid, tool_input=_ai), EB)
    hook("PostToolUse", jbase(BG, BGP, TPBG, tool_name="Agent", tool_use_id=tid, tool_input=_ai,
                              tool_response={"isAsync": True, "status": "async_launched", "agentId": aid,
                                             "description": desc, "prompt": "do it", "outputFile": "/tmp/x"}), EB)
hook("SubagentStart", jbase(BG, BGP, TPBG, agent_id="agentAAA111", agent_type="general-purpose"), EB)
hook("SubagentStart", jbase(BG, BGP, TPBG, agent_id="agentBBB222", agent_type="general-purpose"), EB)
hook("SubagentStop", jbase(BG, BGP, TPBG, agent_id="agentBBB222", agent_type="general-purpose",
                           last_assistant_message="lint clean"), EB)
# 5. grep's exit 1: Claude Code interprets it ("No matches found") and fires
# PostToolUse; and a validation whose last command is such a grep.
for tid, cmd in (("gr1", "grep -q TODO m.py"), ("gr2", "pytest -q | grep -q passed")):
    hook("PreToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id=tid, tool_input={"command": cmd}), EB)
    hook("PostToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id=tid, tool_input={"command": cmd},
                              tool_response={"stdout": "", "stderr": "", "interrupted": False,
                                             "returnCodeInterpretation": "No matches found"}), EB)
# 6. A plain command that worked: no exit code in its result.
hook("PreToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id="pl1", tool_input={"command": "ls -la"}), EB)
hook("PostToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id="pl1", tool_input={"command": "ls -la"},
                          tool_response={"stdout": "total 0", "stderr": "", "interrupted": False}), EB)
capture.ingest(BG)
_o = {r["op"]: r["data"] for r in jrecs(task_of(BG)) if r["kind"] == "tool.outcome"}
check("a Bash result with a backgroundTaskId (run_in_background, moved to deliver a message, Ctrl+B) and an async "
      "Agent: recorded as started in the background, with neither ok nor an exit code",
      lambda: [_o[k].get("background") for k in ("bg1", "bg2", "bg3", "ag1")]
      == ["background", "deliver_message", "user", "async_launched"]
      and not any("ok" in _o[k] or "exit" in _o[k] for k in ("bg1", "bg2", "bg3", "ag1", "ag2", "ag3")))
check("...a result Claude Code interprets (grep's exit 1): ok with its words, no exit code; a plain success: ok, "
      "no exit code (the result states none)",
      lambda: _o["gr1"].get("ok") is True and _o["gr1"].get("interpretation") == "No matches found"
      and "exit" not in _o["gr1"] and _o["pl1"].get("ok") is True and "exit" not in _o["pl1"])
sbg = summary.build(task_of(BG))
dump("background", sbg.text)
_fl, _dn, _vl, _ch = (section(sbg.text, n) for n in ("In flight / unknown", "Done (observed outcomes)", "Validation",
                                                     "Children"))
check("summary: background work is In flight ('started in the background ...; its result was not observed'), "
      "never in Done, never 'passed'",
      lambda: "ckbg-marker`: started in the background (observed, seq" in _fl and "id b4pv3di4t" in _fl
      and "moved to the background so a message could reach the model" in _fl
      and "moved to the background by the user (Ctrl+B)" in _fl and "its result was not observed" in _fl
      and "ckbg-marker" not in _dn and "release-build" not in _dn and "every recorded intent has an outcome" not in _fl)
check("...Validation says started in the background, result not observed (never passed)",
      lambda: re.search(r"test `sleep 40; pytest -q; echo ckbg-marker`: started in the background \(observed, seq "
                        r"[0-9]+, id b4pv3di4t\); its result was not observed", _vl)
      and re.search(r"test `python3 -m pytest -q tests/slow`: moved to the background so a message could reach the "
                    r"model \(observed, seq [0-9]+, id id-bg2\); its result was not observed", _vl)
      and "ckbg-marker`: passed" not in _vl and "tests/slow`: passed" not in _vl)
check("...an async subagent: in flight until its stop is recorded, then in Done (stop recorded, see Children)",
      lambda: "Review the docs`: started in the background (an async subagent)" in _fl
      and "Count the files`: started in the background (an async subagent)" in _fl
      and "Lint the repo`: started in the background (an async subagent)" in _dn
      and "its subagent's stop was recorded" in _dn and "Review the docs" not in _dn)
check("...Children: started (no stop recorded, so its result not observed), stopped, and the one no hook saw",
      lambda: re.search(r"subagent agentAAA111 \(general-purpose\): started \(observed, seq [0-9]+\); no stop recorded, "
                        r"so its result was not observed", _ch)
      and "subagent agentBBB222 (general-purpose): stopped (observed" in _ch
      and re.search(r"subagent agentCCC333 \(Agent `Count the files`\): started in the background \(observed, seq "
                    r"[0-9]+\); no start or stop recorded for it", _ch))
check("...grep's 'No matches found': ok with its words in Done; the validation ending in such a grep is not a clear "
      "pass; no 'exit 0' claimed anywhere",
      lambda: "grep -q TODO m.py`: ok (No matches found)" in _dn
      and "pytest -q | grep -q passed`: ended without an error but said \"No matches found\"" in _vl
      and "ls -la`: ok (observed" in _dn and "exit 0" not in sbg.text)
# A background command whose payload was cut (over 256 KiB): what the prefix
# still says (run_in_background) is enough to keep it a start.
_cbi = {"command": "sleep 30; pytest -q tests/huge", "run_in_background": True, "description": "d" * 300000}
hook("PreToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id="bgcut", tool_input=_cbi), EB)
hook("PostToolUse", jbase(BG, BGP, TPBG, tool_name="Bash", tool_use_id="bgcut", tool_input=_cbi,
                          tool_response={"stdout": "", "stderr": "", "interrupted": False,
                                         "backgroundTaskId": "bcut1"}), EB)
capture.ingest(BG)
_oc = next((r["data"] for r in jrecs(task_of(BG)) if r["kind"] == "tool.outcome" and r["op"] == "bgcut"), {})
_sc = summary.build(task_of(BG)).text
check("...a background command whose payload was cut (over 256 KiB): still a start, in flight, never a failure",
      lambda: _oc.get("payload_cut") and _oc.get("background") == "background" and "ok" not in _oc
      and "tests/huge`: started in the background" in section(_sc, "In flight / unknown")
      and "tests/huge" not in section(_sc, "Failures (observed)") and "tests/huge" not in section(_sc, "Done"))
# A journal written before this was recorded: ok and exit 0 with a background id.
TLEG = "t_legacy_bg"
journal.append(TLEG, [
    {"kind": "tool.intent", "op": "lg1", "session": "sl", "data": {"tool": "Bash", "validation": "test",
                                                                   "input": {"command": "pytest -q", "background": True}}},
    {"kind": "tool.outcome", "op": "lg1", "session": "sl", "data": {"tool": "Bash", "ok": True, "exit": 0,
                                                                    "result": {"background_id": "bx", "exit": 0}}}])
_sl = summary.build(TLEG).text
check("...an older journal's background command (recorded ok, exit 0, with a background id) reads as started, "
      "not passed", lambda: "pytest -q`: started in the background" in section(_sl, "Validation")
      and "passed" not in section(_sl, "Validation") and "pytest -q" in section(_sl, "In flight / unknown"))

# A failed Edit is not a written file.
hook("PreToolUse", jbase(BG, BGP, TPBG, tool_name="Edit", tool_use_id="fe1",
                         tool_input={"file_path": str(BGP / "m.py"), "old_string": "M = 9", "new_string": "M = 2"}), EB)
hook("PostToolUseFailure", jbase(BG, BGP, TPBG, tool_name="Edit", tool_use_id="fe1",
                                 tool_input={"file_path": str(BGP / "m.py"), "old_string": "M = 9",
                                             "new_string": "M = 2"},
                                 error="String to replace not found in file.", is_interrupt=False), EB)
capture.ingest(BG)
sfe = summary.build(task_of(BG)).text
check("a failed Edit: in Failures, not counted as a written file; Files changed says it failed and that no "
      "successful edit was recorded",
      lambda: "file(s) written" not in section(sfe, "Done (observed outcomes)")
      and "m.py`: failed" in section(sfe, "Failures (observed)")
      and "1 edit(s); 1 failed" in section(sfe, "Files changed")
      and "no successful edit recorded" in section(sfe, "Files changed"))
hook("PreToolUse", jbase(BG, BGP, TPBG, tool_name="Edit", tool_use_id="fe2",
                         tool_input={"file_path": str(BGP / "m.py"), "old_string": "M = 1", "new_string": "M = 2"}), EB)
(BGP / "m.py").write_text("M = 2\n")
hook("PostToolUse", jbase(BG, BGP, TPBG, tool_name="Edit", tool_use_id="fe2",
                          tool_input={"file_path": str(BGP / "m.py"), "old_string": "M = 1", "new_string": "M = 2"},
                          tool_response={"userModified": False}), EB)
capture.ingest(BG)
sfe2 = summary.build(task_of(BG)).text
check("...then one that works: '1 file(s) written (inferred from their edits' successful outcomes...)', its after-hash",
      lambda: "- 1 file(s) written (inferred from their edits' successful outcomes" in sfe2
      and f"after {_hashlib.sha256(b'M = 2' + bytes([10])).hexdigest()[:12]}" in section(sfe2, "Files changed")
      and "unchanged since (current hash matches)" in section(sfe2, "Files changed"))

# Staleness outside git: the recorded hashes are compared all the same.
NG = "sess-nogit"
NGP = JT / "nogit proj"
NGP.mkdir()
hook("SessionStart", jbase(NG, NGP, source="startup"), EB)
hook("UserPromptSubmit", jbase(NG, NGP, prompt="Write calc.py and test it", prompt_id="ng0"), EB)
_wi = {"file_path": str(NGP / "calc.py"), "content": "def add(a, b): return a + b\n"}
hook("PreToolUse", jbase(NG, NGP, tool_name="Write", tool_use_id="ngw", tool_input=_wi), EB)
(NGP / "calc.py").write_text(_wi["content"])
hook("PostToolUse", jbase(NG, NGP, tool_name="Write", tool_use_id="ngw", tool_input=_wi,
                          tool_response={"type": "create"}), EB)
hook("PreToolUse", jbase(NG, NGP, tool_name="Bash", tool_use_id="ngt", tool_input={"command": "python -m pytest -q"}), EB)
hook("PostToolUse", jbase(NG, NGP, tool_name="Bash", tool_use_id="ngt", tool_input={"command": "python -m pytest -q"},
                          tool_response={"stdout": "1 passed", "stderr": "", "interrupted": False}), EB)
capture.ingest(NG)
sng1 = section(summary.build(task_of(NG)).text, "Validation")
(NGP / "calc.py").write_text("def add(a, b): return a - b\n")   # changed outside an edit tool (sed, an editor)
sng2 = section(summary.build(task_of(NG)).text, "Validation")
check("outside git: a validation is still current while the file hashes it ran against are unchanged ...",
      lambda: "no git commit" in sng1 and "1 file hash(es)" in sng1 and "still current (inferred: those file hashes" in sng1)
check("...and STALE once one changes (outside any edit tool), naming the file",
      lambda: "STALE (inferred:" in sng2 and "calc.py changed" in sng2 and "still current" not in sng2)

# The version a validation ran against: the hook reads HEAD (without git)
# before the command runs; the snapshot comes a moment later, and if HEAD
# moved meanwhile, the version is uncertain.
VS = "sess-version"
VSP = JT / "version proj"
VSP.mkdir()
jgit(VSP, "init", "-q", "-b", "main")
(VSP / "v.py").write_text("V = 1\n")
jgit(VSP, "add", "v.py")
jgit(VSP, "commit", "-qm", "v1")
jgit(VSP, "checkout", "-qb", "other")
(VSP / "v.py").write_text("V = 9\n")
jgit(VSP, "commit", "-qam", "o")
jgit(VSP, "checkout", "-q", "main")


def _rev(repo, ref="HEAD"):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True,
                          env=JGIT).stdout.strip()


_main = _rev(VSP)
hook("SessionStart", jbase(VS, VSP, source="startup"), EB)
_vcmd = "pytest -q; git checkout -q other"
_vpay = jbase(VS, VSP, tool_name="Bash", tool_use_id="vr1", tool_input={"command": _vcmd})
# The command checks out `other` right after the hook returns, before the
# snapshot: hold the spool's lock (the snapshot ingests first) until it has.
with capture._spool_lock(capture.spool_dir(VS)):
    subprocess.run([SH, str(ROOT / "hooks" / "fast.sh"), "PreToolUse"], input=json.dumps(_vpay), capture_output=True,
                   text=True, timeout=60, env=dict(os.environ, **EB))
    jgit(VSP, "checkout", "-q", "other")
settle_background()
hook("PostToolUse", dict(_vpay, tool_response={"stdout": "1 passed", "stderr": "", "interrupted": False}), EB)
capture.ingest(VS)
_vi = next((r["data"] for r in jrecs(task_of(VS)) if r["kind"] == "tool.intent" and r["op"] == "vr1"), {})
_vv = next((r["data"] for r in jrecs(task_of(VS)) if r["kind"] == "validation.version" and r["op"] == "vr1"), {})
_vs = section(summary.build(task_of(VS)).text, "Validation")
check("a test command's intent holds the HEAD and branch the hook read before it ran (no git process: .git/HEAD "
      "and the ref)", lambda: _vi.get("head_at_start") == {"head": _main, "branch": "main"})
check("...its snapshot, taken after the command moved HEAD, says how long after (lag_ms) and that HEAD moved",
      lambda: _vv.get("hook_head") == _main and _vv.get("head") == _rev(VSP) and _vv.get("head_moved") is True
      and isinstance(_vv.get("lag_ms"), int) and _vv.get("captured") == "after start")
check("...the summary: started on the hook's HEAD (observed), the snapshot's HEAD later, the version uncertain; "
      "never 'observed at its start'",
      lambda: f"started on HEAD {_main[:10]} on main (read by the hook before the command ran: observed)" in _vs
      and f"but HEAD was {_rev(VSP)[:10]} on other by its snapshot ~" in _vs
      and "which one it ran against is uncertain" in _vs and "observed at its start" not in _vs)
# The hook's reading in a worktree (a .git file, commondir), with packed refs, detached.
jgit(VSP, "checkout", "-q", "main")
VWT = JT / "version wt"
jgit(VSP, "worktree", "add", "-q", str(VWT), "other")
jgit(VSP, "pack-refs", "--all")
_heads = []
for tid, where, prep in (("wt1", VWT, None), ("pk1", VSP, None), ("dt1", VSP, ("checkout", "-q", "--detach"))):
    if prep:
        jgit(where, *prep)
    hook("PreToolUse", jbase(VS, where, tool_name="Bash", tool_use_id=tid, tool_input={"command": "make check"}), EB)
    capture.ingest(VS)
    _heads.append(next((r["data"].get("head_at_start") for r in jrecs(task_of(VS))
                        if r["kind"] == "tool.intent" and r["op"] == tid), None))
check("...read right in a worktree (its .git file and commondir), with packed refs, and detached",
      lambda: _heads == [{"head": _rev(VWT), "branch": "other"}, {"head": _rev(VSP), "branch": "main"},
                         {"head": _rev(VSP), "branch": "(detached)"}]
      and not (VSP / ".git" / "refs" / "heads" / "main").exists())
NGV = next((r["data"] for r in jrecs(task_of(NG)) if r["kind"] == "tool.intent" and r["op"] == "ngt"), {})
check("...and nothing outside a repository", lambda: "head_at_start" not in NGV and NGV.get("validation") == "test")

# --- checkpoints ---------------------------------------------------------------------------
print("checkpoints (/check:save, /check-list):")
out = hook("UserPromptSubmit", jbase(SM, SMP, TPS, prompt="/check:save before deploy", prompt_id="sv1"), ES)
cps = [r for r in jrecs(task_of(SM)) if r["kind"] == "checkpoint"]
cpd = cps[-1]["data"] if cps else {}
side = state.read_json(journal.jdir(task_of(SM)) / "checkpoints" / f"{cpd.get('id')}.json", {})
check("/check:save: 'saved and synced to disk', and a checkpoint record: id, seq range, summary hash, file hashes, git",
      lambda: "saved and synced to disk" in ctx(out) and cpd.get("id", "").startswith("c_") and cpd["seq_to"] >= cpd["seq_from"] > 0
      and cpd["reason"] == "before deploy" and cpd["files"].get(str(SMP / "lib.py")) == capture.file_hash(SMP / "lib.py")
      and len(cpd["git"]["head"]) == 40 and side.get("summary_sha256") == cpd["summary_sha256"]
      and sanitize.sha256_text(side["summary"]) == cpd["summary_sha256"])
check("...it never pauses or stops anything", lambda: not state.is_paused(SM))
lst = core.list_checkpoints(SM)
check("/check-list (run.py list): the task's checkpoints with id, seq range, HEAD and summary hash",
      lambda: cpd.get("id", "?") in lst and f"seq {cpd.get('seq_from')}-{cpd.get('seq_to')}" in lst)
_syncs = []
journal._durable = lambda fd, full: (_syncs.append(full), _real_durable(fd, full))[1]
ok_, msg_ = core.save(SM, "counted")
journal._durable = _real_durable
check("save syncs to disk (a full sync) before it says saved", lambda: ok_ and True in _syncs and "saved" in msg_)


def _nosync(fd, full):
    raise OSError(_errno.EIO, "Input/output error")


journal._durable = _nosync
ok_, msg_ = core.save(SM, "must fail")
journal._durable = _real_durable
check("a sync that fails: 'NOT saved', never 'saved'", lambda: not ok_ and "NOT saved" in msg_ and "saved and synced" not in msg_)
capture.ingest(SM)

# --- sessions, lineage, the kill-and-recover path --------------------------------------------
print("lineage and recovery after a killed session (no Stop, no handoff):")
KP = JT / "kill proj"
KP.mkdir()
jgit(KP, "init", "-q")
(KP / "w.py").write_text("old\n")
jgit(KP, "add", "w.py")
jgit(KP, "commit", "-qm", "c")
CK1 = fake_claude()
EK1 = {"CLAUDE_PID": str(CK1.pid), **HENV}
SA = "sess-killed-aaaa"
TPA = JT / "ta-kill.jsonl"
TPA.write_text("")
hook("SessionStart", jbase(SA, KP, TPA, source="startup"), EK1)
hook("UserPromptSubmit", jbase(SA, KP, TPA, prompt="Port w.py to the new format", prompt_id="k1"), EK1)
mcp([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
     {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "checkpoint_note", "arguments": {
         "kind": "constraint", "text": "Do not change the public function names"}}}],
    dict(os.environ, CLAUDE_PID=str(CK1.pid), CLAUDE_CODE_SESSION_ID=SA))
hook("PreToolUse", jbase(SA, KP, TPA, tool_name="TaskCreate", tool_use_id="kc1", tool_input={"subject": "Port w.py"}), EK1)
hook("PostToolUse", jbase(SA, KP, TPA, tool_name="TaskCreate", tool_use_id="kc1", tool_input={"subject": "Port w.py"},
                          tool_response={"task": {"id": "1", "subject": "Port w.py"}}), EK1)
hook("PreToolUse", jbase(SA, KP, TPA, tool_name="Write", tool_use_id="kw1",
                         tool_input={"file_path": str(KP / "w.py"), "content": "new\n"}), EK1)
(KP / "w.py").write_text("new\n")
hook("PostToolUse", jbase(SA, KP, TPA, tool_name="Write", tool_use_id="kw1",
                          tool_input={"file_path": str(KP / "w.py"), "content": "new\n"}, tool_response={"type": "update"}), EK1)
hook("PreToolUse", jbase(SA, KP, TPA, tool_name="Bash", tool_use_id="kt1", tool_input={"command": "sh run_tests.sh"}), EK1)
hook("PostToolUse", jbase(SA, KP, TPA, tool_name="Bash", tool_use_id="kt1", tool_input={"command": "sh run_tests.sh"},
                          tool_response={"stdout": "ok", "stderr": "", "interrupted": False}), EK1)
hook("PreToolUse", jbase(SA, KP, TPA, tool_name="Bash", tool_use_id="kfly",
                         tool_input={"command": "rsync -a out/ prod:/srv/app"}), EK1)
TA_ = task_of(SA)
CK1.kill()   # SIGKILL: no Stop, no SessionEnd, the spool not ingested since the last Python moment
CK1.wait()
check("killed mid-task: its last events are still in its spool, not yet in the journal",
      lambda: spooled(SA) and not any(r["op"] == "kfly" for r in jrecs(TA_)))
CK2 = fake_claude()
EK2 = {"CLAUDE_PID": str(CK2.pid), **HENV}
SB = "sess-next-bbbb"
TPB = JT / "tb-kill.jsonl"
TPB.write_text("")
out = hook("SessionStart", jbase(SB, KP, TPB, source="startup"), EK2)
check("a new session in that project: the dead session's spool is ingested, and its task is offered as interrupted",
      lambda: not spooled(SA) and any(r["op"] == "kfly" for r in jrecs(TA_))
      and TA_ in ctx(out) and "interrupted (no clean end)" in ctx(out) and "/check:resume" in ctx(out)
      and task_of(SB) != TA_)
out = hook("UserPromptSubmit", jbase(SB, KP, TPB, prompt=f"/check:resume {TA_}", prompt_id="r1"), EK2)
rc_ = ctx(out)
check("/check:resume <task>: the recovery summary has the goal, the constraint, progress, the file with its hash, "
      "the validation with its version, and the in-flight operation as outcome unknown",
      lambda: "Port w.py to the new format" in rc_ and "Do not change the public function names" in rc_
      and "[pending] Port w.py" in rc_ and str(KP / "w.py") in rc_
      and _hashlib.sha256(b"new\n").hexdigest()[:12] in rc_
      and "test `sh run_tests.sh`: passed" in rc_ and "started on HEAD" in rc_
      and "rsync -a out/ prod:/srv/app" in rc_ and "outcome unknown" in rc_)
check("...and the new session adopts the task (lineage: session.adopt naming the old task and its last session)",
      lambda: task_of(SB) == TA_ and any(r["kind"] == "session.adopt" and r["data"]["parent"] == SA for r in jrecs(TA_)))
# /clear: a new session id in the same process, a new task, the old session its parent.
SCL = "sess-after-clear"
hook("SessionEnd", jbase(SB, KP, TPB, reason="clear"), EK2)
out = hook("SessionStart", jbase(SCL, KP, TPB, source="clear"), EK2)
st_ = next(r for r in jrecs(task_of(SCL)) if r["kind"] == "session.start")
check("/clear: a new task whose session.start names the previous session of the same process as its parent",
      lambda: task_of(SCL) != TA_ and st_["data"]["source"] == "clear" and st_["data"]["parent"] == SB
      and any(r["kind"] == "session.end" and r["data"]["reason"] == "clear" for r in jrecs(TA_)))
# resume of the same session id: same task, and the summary comes back when it was interrupted.
CK3 = fake_claude()
hook("SessionStart", jbase(SA, KP, TPA, source="resume"), {"CLAUDE_PID": str(CK3.pid), **HENV})
check("claude --resume of the killed session (same id): same task, the session.start says resume",
      lambda: task_of(SA) == TA_ and any(r["kind"] == "session.start" and r["data"]["source"] == "resume" and r["session"] == SA
                                 for r in jrecs(TA_)))
CK4 = fake_claude()
SR = "sess-resumed-again"
hook("SessionStart", jbase(SR, KP, TPA, source="startup"), {"CLAUDE_PID": str(CK4.pid), **HENV})
CK4.kill()
CK4.wait()
state.update_session(SR, task_id=TA_)
SRT = JT / "sr.jsonl"
SRT.write_text(json.dumps({"sessionId": SR, "type": "user"}) + "\n")
SF = "sess-forked"
out = hook("SessionStart", jbase(SF, KP, SRT, source="fork"), {"CLAUDE_PID": str(CK3.pid), **HENV})
check("a fork (new id, the transcript's earlier session id): continues that session's task, parent named from the transcript",
      lambda: task_of(SF) == TA_ and any(r["kind"] == "session.start" and r["session"] == SF and r["data"].get("parent") == SR
                                 and r["data"].get("parent_how") == "transcript" for r in jrecs(TA_))
      and "<checkpoint-recovery>" in ctx(out))
out = hook("SessionStart", jbase(SF, KP, SRT, source="compact"), {"CLAUDE_PID": str(CK3.pid), **HENV})
check("after a compaction (SessionStart compact): the recovery summary is in the context", lambda: "<checkpoint-recovery>" in ctx(out)
      and "Port w.py to the new format" in ctx(out))

# Canary scan: nothing of the synthetic secrets anywhere in checkpoint's state.
print("no synthetic secret anywhere in checkpoint's state:")
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="sec1",
                         tool_input={"command": f"GITHUB_TOKEN={S['github classic']} git push https://x:{S['password']}@h/r"}), EC)
hook("PostToolUse", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="sec1", tool_input={"command": "x"},
                          tool_response={"stdout": f"Authorization: Bearer {S['token']}", "stderr": S["jwt"],
                                         "interrupted": False}), EC)
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="mcp__other__login", tool_use_id="sec2",
                         tool_input={"password": S["password"], "api_key": S["anthropic"], "note": S["hugging face"]}), EC)
hook("UserPromptSubmit", jbase(SC, CP, TPC, prompt=f"use {S['aws secret']} as aws_secret_access_key={S['aws secret']} "
                               f"and {S['slack']}", prompt_id="sec3"), EC)
# Names without separators, end to end (prompt, command, output).
hook("UserPromptSubmit", jbase(SC, CP, TPC, prompt=f"use APITOKEN={S['apitoken']} for the api", prompt_id="sec4"), EC)
_pgc = f"PGPASSWORD={S['pgpassword']} psql -h db -c 'select 1'"
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="sec5", tool_input={"command": _pgc}), EC)
hook("PostToolUse", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="sec5", tool_input={"command": _pgc},
                          tool_response={"stdout": f"connecting with pgpassword={S['pgpassword']}", "stderr": "",
                                         "interrupted": False}), EC)
_mix = (f"DBPASSWORD={S['dbpassword']} GITHUBTOKEN={S['githubtoken']} PASSWORD={S['comma head']},{S['comma tail']} "
        f"DB_PASS={S['db_pass']} make test")
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="sec6", tool_input={"command": _mix}), EC)
hook("PostToolUse", jbase(SC, CP, TPC, tool_name="Bash", tool_use_id="sec6", tool_input={"command": _mix},
                          tool_response={"stdout": f'REDISPASSWORD: {S["redispassword"]}\n{{"pgpassword": '
                                                   f'"{S["json pgpassword"]}"}}', "stderr": "", "interrupted": False}), EC)
hook("PreToolUse", jbase(SC, CP, TPC, tool_name="mcp__other__connect", tool_use_id="sec7",
                         tool_input={"dbpassword": S["json dbpassword"], "host": "db"}), EC)
# File contents with a secret no pattern knows (.pgpass): Read, Grep, WebFetch,
# an MCP tool's result, and a plain `cat` through Bash.
FILE_CANARY = "db.internal:5432:*:admin:" + _rand(26)
(CP / ".pgpass").write_text(FILE_CANARY + "\n")
for tid, tool, inp, resp in (
        ("fc1", "Read", {"file_path": str(CP / ".pgpass")},
         {"type": "text", "file": {"filePath": str(CP / ".pgpass"), "content": FILE_CANARY + "\n", "numLines": 1,
                                   "startLine": 1, "totalLines": 1}}),
        ("fc2", "Grep", {"pattern": "admin", "path": str(CP), "output_mode": "content"},
         {"mode": "content", "numFiles": 1, "filenames": [str(CP / ".pgpass")], "content": FILE_CANARY, "numLines": 1}),
        ("fc3", "WebFetch", {"url": "https://example.invalid/cfg", "prompt": "show it"},
         {"code": 200, "codeText": "OK", "bytes": 60, "result": FILE_CANARY, "durationMs": 5,
          "url": "https://example.invalid/cfg"}),
        ("fc4", "mcp__other__read", {"path": "/x"}, [{"type": "text", "text": FILE_CANARY}]),
        ("fc5", "Bash", {"command": f"cat {CP / '.pgpass'}"},
         {"stdout": FILE_CANARY, "stderr": "", "interrupted": False})):
    hook("PreToolUse", jbase(SC, CP, TPC, tool_name=tool, tool_use_id=tid, tool_input=inp), EC)
    hook("PostToolUse", jbase(SC, CP, TPC, tool_name=tool, tool_use_id=tid, tool_input=inp, tool_response=resp), EC)
capture.ingest(SC)
_rd = next((r["data"] for r in jrecs(TC) if r["kind"] == "tool.outcome" and r["op"] == "fc1"), {})
check("a Read of a file: its outcome keeps the path, line count and size, not the content",
      lambda: _rd.get("result", {}).get("file_path") == str(CP / ".pgpass") and _rd["result"].get("lines") == 1
      and _rd["result"].get("content_chars") == len(FILE_CANARY) + 1)
_fcv = FILE_CANARY.rsplit(":", 1)[1]
fc_hits = scan_for([_fcv, _fcv[:10]], state.home())   # its first characters too: a clipped copy counts
check("file contents read through Read, Grep, WebFetch, an MCP tool and a `cat` (a secret no pattern knows): "
      "nothing of it in CHECKPOINT_HOME after ingest", lambda: not fc_hits)
if fc_hits:
    print(f"       (in: {fc_hits[:5]})")
hits = scan_for([S["github classic"], S["password"], S["token"], S["jwt"], S["anthropic"], S["hugging face"],
                 S["slack"], S["apitoken"], S["pgpassword"], S["dbpassword"], S["githubtoken"], S["comma head"],
                 S["comma tail"], S["db_pass"], S["redispassword"], S["json pgpassword"], S["json dbpassword"]],
                state.home())
check("after ingesting tool calls, outputs, MCP calls and prompts holding secrets (names without separators such as "
      "PGPASSWORD/APITOKEN, lowercase JSON keys, a value with a comma too): no file in CHECKPOINT_HOME holds one "
      "(journal, meta, heads, checkpoints, spool remnants)", lambda: not hits)
if hits:
    print(f"       (in: {hits[:5]})")

# --- a checkpoint that isn't this session's ----------------------------------------------
print("a resume uses only this session's own pause-time checkpoint:")
MP = JT / "own-cp"
MP.mkdir()
for _n, _who in (("sess-cp-other", "sess-somebody-else"), ("sess-cp-nosid", None)):
    _f = state.p("checkpoints", _n + ".json")
    state.write_json(_f, {**({"session_id": _who} if _who else {}), "paused_ts": time.time() - 60,
                          "interrupted": [{"command": f"OTHER-CMD-{_n}", "tool_use_id": "x"}],
                          "stages_in_progress": [f"9. NOT-MINE-{_n}"]})
    state.write_json(state.session_path(_n), {"session_id": _n, "cwd": str(MP), "claude_pid": MAIN_CLAUDE.pid,
                                              "paused": True, "paused_ts": time.time() - 60, "checkpoint": str(_f)})
    state.touch(state.p("flags", _n), state.now_iso())
    out = hook("UserPromptSubmit", jbase(_n, MP, prompt="continue please"))
    check(f"...one written by {'another session' if _who else 'no session (no session_id)'}: resumed, and nothing "
          "of it in the resume context", lambda: "<checkpoint-resume>" in ctx(out) and not state.is_paused(_n)
          and f"OTHER-CMD-{_n}" not in ctx(out) and f"NOT-MINE-{_n}" not in ctx(out))

# --- overhead ------------------------------------------------------------------------------
print("overhead (measured):")
NOSPOOL = plugin_copy(TMP / "nospool-copy")
WITH = plugin_copy(TMP / "spool-copy")
_fs = (NOSPOOL / "hooks" / "fast.sh").read_text()
(NOSPOOL / "hooks" / "fast.sh").write_text(re.sub(r"^(\s*)ck_spool .*$", r"\1:", _fs, flags=re.M))


def _timed(script, event, payload, n, env_=None):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run([SH, str(script), event], input=json.dumps(payload), capture_output=True, text=True,
                       env=dict(os.environ, **(env_ or {})))
        ts.append(time.perf_counter() - t0)
    ts.sort()
    return ts[len(ts) // 2] * 1000


def _median(v):
    return sorted(v)[len(v) // 2]


def _added(base, with_):
    """The cost one copy adds over the other: the median of the differences of calls timed in pairs."""
    return _median([w - b for b, w in zip(base, with_)])


# Two clusters of hook times (80 and 180 ms) and one pair split across them: 1 ms added, not the 101 ms
# between the two medians.
_pb, _pw = [80] * 20 + [180] * 19 + [80], [81] * 20 + [181] * 19 + [181]
check("the spool's added cost is the median of the paired differences, not the difference of the medians",
      _added(_pb, _pw) == 1 and _median(_pw) - _median(_pb) == 101)


def _timed_pair(event, payload, n, env_=None):
    """n pairs of calls of NOSPOOL's and WITH's fast.sh, back to back (the order flips each pair): both
    medians (ms) and the cost WITH adds, the median of the pairs' differences. A loaded machine's speed
    drifts by 2x within seconds and its hook times fall into clusters: taken in turns, the medians of two
    identical copies still differed by up to 39 ms, the median of their pairs' differences by at most 2 ms
    (the two calls of a pair share the moment)."""
    ts = ([], [])
    for i in range(n):
        for k in ((0, 1) if i % 2 else (1, 0)):
            script = (NOSPOOL, WITH)[k] / "hooks" / "fast.sh"
            t0 = time.perf_counter()
            subprocess.run([SH, str(script), event], input=json.dumps(payload), capture_output=True, text=True,
                           env=dict(os.environ, **(env_ or {})))
            ts[k].append((time.perf_counter() - t0) * 1000)
    return _median(ts[0]), _median(ts[1]), _added(*ts)


def _translated():
    """Whether this process runs under Rosetta (an x86_64 build on Apple silicon): `sysctl -n
    sysctl.proc_translated` prints 1. The hooks it starts run translated too. False off macOS. The
    system's sysctl by its path: one found on PATH could say anything."""
    try:
        r = subprocess.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "1"


TRANSLATED = sys.platform == "darwin" and _translated()
# A translated process sees an x86_64 machine; an arm64 (or any other) interpreter is native, and a detector
# that called it translated would drop both budgets on every run.
check(f"the Rosetta detection never takes a native interpreter for translated (this run: "
      f"{_platform.machine()}, {'translated' if TRANSLATED else 'native'})",
      not TRANSLATED or _platform.machine() == "x86_64")
_shim = TMP / "sysctl-shim"
_shim.mkdir()
(_shim / "sysctl").write_text("#!/bin/sh\necho 1\n")
(_shim / "sysctl").chmod(0o755)
_path = os.environ["PATH"]
os.environ["PATH"] = f"{_shim}{os.pathsep}{_path}"
try:
    _shimmed = _translated()
finally:
    os.environ["PATH"] = _path
check("...and asks the system's sysctl: one on PATH that prints 1 changes nothing",
      _shimmed == TRANSLATED)


def _within_budget(name, measure, translated=TRANSLATED):
    """The spool's budget (median added <= 10 ms per hook) as a check on measure() (the added ms), measured
    again, twice at most, while over it: under other processes' load the spool's file writes can stall for a
    while (two dash runs measured 11 and 16 ms as installed, the same set-up probed minutes later under
    1 ms), and a cost that is really there shows in every try. Translated, a note with the number: under
    Rosetta every hook takes ~6x as long, and two identical copies still differ by more than 10 ms (the
    median of 40 pairs' differences reached 10.8 ms), so the budget can't tell the spool's cost from noise
    there."""
    added = [measure()]
    if translated:
        print(f"  note: under Rosetta, not held to the budget: {name} (added {added[0]:.1f} ms)")
        return
    while added[-1] > 10 and len(added) < 3:
        print(f"       over budget ({added[-1]:.1f} ms added): measured again")
        added.append(measure())
    check(name, added[-1] <= 10)


_out, _counts = io.StringIO(), (PASS, FAIL)
with contextlib.redirect_stdout(_out):
    _within_budget("a stand-in 50 ms over", lambda: 50.0, translated=True)
if not _out.getvalue().startswith("  note: under Rosetta"):
    print(_out.getvalue(), end="")   # what it printed instead: a FAIL, already counted
check(f"under Rosetta an overhead over budget is a note, not a failure (this run: "
      f"{'translated' if TRANSLATED else 'native'})",
      lambda: (PASS, FAIL) == _counts and _out.getvalue().startswith("  note: under Rosetta"))
# ...and natively it is a check (run with a recorder in check's place; each stand-in keeps what it wasn't asked).
_check, _verdicts, _out = check, [], io.StringIO()
_tries = {"10.5 each time": [10.5, 10.5, 10.5, 0.0], "10": [10.0, 99.0], "12, 11, then 4": [12.0, 11.0, 4.0, 99.0],
          "12, 11, 13": [12.0, 11.0, 13.0, 1.0]}
check = lambda name, cond: _verdicts.append(bool(cond() if callable(cond) else cond))   # noqa: E731
try:
    with contextlib.redirect_stdout(_out):
        for _k, _v in _tries.items():
            _within_budget(f"a stand-in: {_k}", lambda _v=_v: _v.pop(0), translated=False)
finally:
    check = _check
check("natively an overhead budget is a check, measured again (twice at most) while over 10 ms: 10.5 ms each time "
      "fails, 10 passes at once, 12, 11 then 4 passes, 12, 11, 13 fails; no note stands in",
      lambda: _verdicts == [False, True, True, False] and [len(v) for v in _tries.values()] == [1, 1, 1, 1]
      and "note:" not in _out.getvalue())

SP = "sess-perf"
pay = jbase(SP, PROJ, tool_name="Read", tool_use_id="perf1", tool_input={"file_path": "/x/y.py"})
# The fast path only: with the suite's CHECKPOINT_NUDGE_EVERY=3 every third PostToolUse of both copies
# would go through Python (hook-post and an ingest, 130-210 ms each).
_fast_env = {"CHECKPOINT_NUDGE_EVERY": "100000"}


def _overhead(label, env_):
    """The spool's added cost per hook (ms): the larger of PreToolUse's and PostToolUse's; printed."""
    (bp, wp, ap), (bq, wq, aq) = (_timed_pair(ev, pay, 40, env_) for ev in ("PreToolUse", "PostToolUse"))
    print(f"       {label}, medians of 40 pairs ({SH}): PreToolUse {bp:.1f} -> {wp:.1f} ms (added {ap:.1f}), "
          f"PostToolUse {bq:.1f} -> {wq:.1f} ms (added {aq:.1f})")
    return max(ap, aq)


for _ in range(3):   # warm caches
    _timed(WITH / "hooks" / "fast.sh", "PreToolUse", pay, 3, _fast_env)
    _timed(NOSPOOL / "hooks" / "fast.sh", "PreToolUse", pay, 3, _fast_env)
_n0 = pycalls()
_within_budget("the spool's added cost on the fast path is small (median added <= 10 ms per hook)",
               lambda: _overhead("fast path", _fast_env))
_fast_py = pycalls() - _n0
check("the fast path, spool included, starts no Python in any of the timed hooks", lambda: _fast_py == 0)
# As installed: no CHECKPOINT_PYTHON, a runtime ready (recorded in rt/<session>). The spool then asks whether
# its runtime is there (ck_body: a `read` and two file tests) and keeps its size count (.size): no process either.
_perf_data = TMP / "perf data"
_perf_py = _perf_data / "envs" / LOCK_SHA / ".pixi" / "envs" / "default" / "bin" / "python3"
_perf_py.parent.mkdir(parents=True)
_perf_py.symlink_to(sys.executable)
(_perf_data / "envs" / LOCK_SHA / ".ready").write_text("")
state.touch(state.p("rt", SP), str(_perf_data / "envs" / LOCK_SHA))
_perf_env = {"CHECKPOINT_PYTHON": "", "CLAUDE_PLUGIN_DATA": str(_perf_data), "CHECKPOINT_NUDGE_EVERY": "100000"}
for _ in range(3):
    _timed(WITH / "hooks" / "fast.sh", "PostToolUse", pay, 3, _perf_env)
    _timed(NOSPOOL / "hooks" / "fast.sh", "PostToolUse", pay, 3, _perf_env)
_within_budget("...as installed: the spool's whole cost, the runtime check and the size count included, stays as "
               "small (median added <= 10 ms per hook)",
               lambda: _overhead("...as installed (runtime ready, no CHECKPOINT_PYTHON)", _perf_env))
check("...as installed (a ready runtime, no CHECKPOINT_PYTHON): the spool keeps its size count and whole "
      "payloads (no metadata-only records)",
      lambda: (capture.spool_dir(SP) / ".size").exists() and not any(ln.endswith("\tmeta=1") for ln in index_lines(SP)))
# Ingest cost: 1000 spooled events of one session, written the way ck_spool writes them.
SI = "sess-ingest-perf"
d_ = capture.spool_dir(SI)
d_.mkdir(parents=True, mode=0o700)
with open(d_ / "index", "w") as idx:
    for i in range(1000):
        ev_ = "PreToolUse" if i % 2 == 0 else "PostToolUse"
        pl = jbase(SI, PROJ, tool_name="Bash", tool_use_id=f"ip{i // 2}", tool_input={"command": f"echo {i}"},
                   **({"tool_response": {"stdout": str(i), "stderr": "", "interrupted": False}} if i % 2 else {}))
        txt_ = json.dumps(pl)
        (d_ / f"{i}.ev").write_text(txt_ + "\n")
        idx.write(f"1\t{ev_}\t{i}.ev\t{len(txt_)}\n")
t0 = time.perf_counter()
ri = capture.ingest(SI)
ingest_s = time.perf_counter() - t0
print(f"       ingest: {ri.events} events -> {ri.records} records in {ingest_s * 1000:.0f} ms "
      f"({ri.events / max(ingest_s, 1e-6):.0f} events/s)")
check("ingesting 1000 spooled events takes well under 5 s, and leaves no payload behind",
      lambda: ri.records == 1000 and ingest_s < 5 and not spooled(SI))
# Bounded growth: the index rotates once ingested past INDEX_ROTATE, and the rotated one goes.
_rot, _keep = capture.INDEX_ROTATE, capture.ROTATED_KEEP_SECONDS
capture.INDEX_ROTATE, capture.ROTATED_KEEP_SECONDS = 600, 0
SG = "sess-growth"
for i in range(40):
    hook("PostToolUse", jbase(SG, PROJ, tool_name="Read", tool_use_id=f"gr{i}", tool_input={"file_path": "x"}))
capture.ingest(SG)
rotated = sorted(p.name for p in capture.spool_dir(SG).glob("index.*"))
for i in range(3):
    hook("PostToolUse", jbase(SG, PROJ, tool_name="Read", tool_use_id=f"gr-b{i}", tool_input={"file_path": "x"}))
time.sleep(0.05)
capture.ingest(SG)
check("bounded spool: payloads deleted once journaled; the index rotates past its limit and the rotated one is "
      "dropped once ingested and quiet",
      lambda: rotated and not list(capture.spool_dir(SG).glob("index.*")) and not spooled(SG)
      and (capture.spool_dir(SG) / "index").stat().st_size < 600
      and len([r for r in jrecs(task_of(SG)) if r["op"].startswith("gr")]) == 43)
capture.INDEX_ROTATE, capture.ROTATED_KEEP_SECONDS = _rot, _keep
