# What the scrubber masks, and what it misses

> Developer reference for checkpoint 0.9.0 (`src/checkpoint/sanitize.py`). The short version is in the README's [Security and privacy](../../README.md#security-and-privacy).

Everything checkpoint writes to its journal, to a pause-time checkpoint
(`journal/<task>/checkpoint-<session>.json`) and to an export goes through
`sanitize.scrub` first. It works on patterns. It holds
no list of your real secrets to compare against, so a secret in a form it
doesn't know stays as it is. That is why every export starts with a line
asking you to review it before sharing.

## Masked

Each value below becomes `***`; the name or option before it stays.

- **Token shapes:** GitHub (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
  `github_pat_`), GitLab `glpat-`, Hugging Face `hf_`, OpenAI and Anthropic
  `sk-...`, Stripe `sk_`/`rk_`, Slack `xox?-`, AWS key ids (`AKIA...`,
  `ASIA...`), Google `AIza...`, npm `npm_`, PyPI `pypi-`, age secret keys,
  JWTs, PEM private key blocks, Telegram bot tokens.
- **Credentials by context:** passwords in URLs (`https://user:pw@host`, also
  `redis://:pw@host`), `Authorization:` and `Bearer` headers, `--password x`
  style flags, assignments or fields whose name says key, token, secret,
  password, credential, auth or cookie (`GH_TOKEN=...`, `"api_key": "..."`,
  `PGPASSWORD=...`, `APITOKEN=...`), and a 40-character AWS secret key near
  words like `aws ... secret` or `aws ... access`.
- **Command-line credentials** that only the program makes secret:

  | Program | Masked |
  |---|---|
  | mysql, mariadb, mongo, mongosh, mongodump ... | `-p<pw>`, `-p <pw>` |
  | curl | `-u`/`--user user:<pw>` (also bundled: `-su`, `-fsSLu`), `-E`/`--cert file:<pw>` |
  | wget | `--user user:<pw>`, `--proxy-user user:<pw>` (its `-U` is the user agent, so it stays) |
  | sshpass | `-p <pw>` (before its command, so `ssh -p 2222` stays) |
  | docker, podman, helm, nerdctl, buildah, skopeo, oras, az | `login -p <pw>` (login as the subcommand) |
  | redis-cli | `-a <pw>` |
  | htpasswd | `-b`'s password |
  | openssl | `-k`, `-K`, `-pass` (`-pass pass:pw` becomes `-pass ***`), `-passin pass:`, `-passout pass:` |
  | ldapsearch and the other ldap tools | `-w <pw>` (also bundled: `-xw`) |
  | smbclient, rpcclient, net ... | `-U user%<pw>`, smbclient's password after `//server/share` |
  | `echo`/`printf` piped into `sudo -S`, `--password-stdin` or `chpasswd` | what is echoed |

  These are found after any separator (`&&`, `;`, `|`, a newline), across
  line continuations, inside a command quoted in another (`ssh host '...'`,
  `sh -c "..."`, `docker exec db sh -c '...'`, `su -c`, `eval`, `watch`,
  `tmux`), and inside `$(...)` and backticks, up to 8 levels deep. A
  `# comment` and a here-document's body don't hide the lines after them, and
  a contraction (`can't`) doesn't open a quote. Each line is then read again
  on its own, and so is each run of lines joined by `\`.

Examples (real output of the 0.8.0 scrubber):

```
mysql -u root -pS3cret app                  ->  mysql -u root -p*** app
curl -fsSu alice:S3cret https://h/x         ->  curl -fsSu alice:*** https://h/x
echo S3cret | sudo -S apt update            ->  echo *** | sudo -S apt update
x=$(mysql -pS3cret -e 'select 1')           ->  x=$(mysql -p*** -e 'select 1')
PGPASSWORD=S3cret psql -h db                ->  PGPASSWORD=*** psql -h db
```

## Not masked

Ordinary forms:

- A secret with no known shape and no telling name next to it:
  `the password is hunter2xyz` stays (`password: hunter2xyz` is masked).
  A bare 40-character AWS secret key with no `aws ... secret/access` near it
  stays too.
- A program named through a variable or an alias: `$MYSQL -pS3cret`.
- A password fed through a here-string or here-document:
  `ldapmodify -y /dev/stdin <<< pw`, `cat <<EOF | sudo -S ...`.
- A command inside another language's string:
  `python -c "os.system('mysql -pS3cret')"`, `fish --command`, `pwsh -c`.
- A command assembled at run time: `bash -c "$(printf 'mysql -p%s' X)"`.
- GNU parallel's command string: `parallel 'mysql -p...' ::: a b`.
- A single-quoted command inside double quotes: `ssh h bash -c "'mysql -p...'"`.
- A stray apostrophe on a line (`O'Neil`, `o'clock`, `'90s`) followed by a
  quote around a command hides that command.

Exotic forms:

- Command lines nested more than 8 levels deep.
- Inside `$(...)`: `case x in a)`, a `#` comment, or a non-contraction
  apostrophe is not bounded while the substitution itself is read.
- `\'` inside `$'...'`.
- A line that starts inside a multi-line quote whose quote pairs with another
  on the same line.
- Unusual here-documents: a terminator line with trailing spaces or `\r\n`,
  or a `<<$'EOF'` delimiter (the body is read on as unterminated); a
  here-document opened inside `$(...)` whose body follows the outer line
  (read as commands).
- A quote opened right after a letter whose text starts like a contraction
  ending and a space (`x'm ...'`): taken as an apostrophe.
- An option more than 256 words after its program (su, shell, ssh, vagrant,
  sudo options too).
- A URL scheme longer than 64 characters glued to `://`.
- smbclient's password when the share isn't written as `//server/share`.
- A bare Telegram token of non-standard length.

## Masked too much

These cost a word of the record, not a secret:

- `mysql -p mydb` becomes `mysql -p ***` (mysql's prompt form loses the
  database name).
- A line in a commit message, a here-document or a multi-line string is read
  as a command line: `fix: mysql -p handling` becomes `fix: mysql -p ***`.
- `curl --cert` masks from the first colon, even when the file name has an
  escaped one.
- Reconciliation compares the masked command with the process table, so a
  masked command is never found still running; it is reported as an unknown
  outcome instead.

## Not scrubbed at all

- Your trackers (`.work/<task>/TRACKER.md`): Claude writes them, checkpoint
  only reads them.
- The spool: Claude Code's raw hook payloads, until they are journaled
  (seconds to a turn; only while checkpoint's runtime is ready or being set
  up). Private files (0600).
- The log of a job checkpoint's own runner runs (`jobs/<id>/output.log`, the
  last 32-64 MB). `job_tail` shows Claude its end as a command's output would
  be shown (the gateway, when installed, masks it); `/check:stop` and the
  resume notes never show it.
