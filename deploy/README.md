# DEV-032 — systemd service users and sandbox, rollout runbook

> Status: **applied to the ECS on 2026-07-31**; evidence in
> `docs/evidence/DEV032_DEV033_部署验收_2026-07-31.md`.
> Target: `deploy@192.0.2.10` (`ssh -i ~/.ssh/personal_agent_example_key`).
> Acceptance (Phase1 拆解 Wave 5): 两用户不能互读 DB/secret；无 root；个人站无回归。
> Design: `docs/Phase1技术方案_v0.1.md` §3.2 (deployment form) and §10.1–10.2
> (sandbox baseline, credentials).

## What this lays down

| Path / name | Owner | Mode | Purpose |
|---|---|---|---|
| user `personal-agent-api` | — | nologin | runs the Client API |
| user `personal-data-mcp` | — | nologin | runs the Finance MCP |
| `/var/lib/personal-agent-api` | api:api | 0700 | `agent.sqlite` |
| `/var/lib/personal-agent-api/*` | api:api or api:**www-data** | 0600 | `-wal`/`-shm` take the unit's `Group=`; see the chmod note under Upgrades |
| `/var/lib/personal-data-mcp` | mcp:mcp | 0700 | `finance.sqlite`, ledger config |
| `/var/lib/personal-data-mcp/*` | mcp:mcp | 0600 | |
| `/etc/personal-agent/api.env` | root:api | 0640 | API environment (incl. GLM key) |
| `/etc/personal-agent/mcp.env` | root:mcp | 0640 | MCP environment (Feishu) |
| `/etc/personal-agent/keys/api/*` | root:api | 0640 | API key rings (private) |
| `/etc/personal-agent/keys/mcp/*` | root:mcp | 0640 | MCP payload key + Host public key |
| `/opt/personal-agent` | deploy | 0755 | wheel, locked deps, `.venv` |
| `/run/personal-agent` (socket dir) | api:www-data | 0770 | the UDS access boundary |
| `/run/personal-agent/api.sock` | api:www-data | 0666 | uvicorn hardcodes 0666; the dir is the boundary |

Deliberate choices, and why:

- **Env/key files are root-owned, group-readable by exactly one service user.**
  A service cannot rewrite its own configuration, and the two users cannot read
  each other's material. systemd reads `EnvironmentFile=` as root before
  dropping privileges, so this works. The *key rings* are different: the
  service process reads those itself, at runtime, under its own credentials —
  which is why the next point matters.
- **`Group=www-data` on the API unit** exists for exactly one reason: the
  socket must be reachable by Nginx. uvicorn hardcodes a fresh UDS to mode
  0666 and ignores umask (verified against the installed uvicorn on
  2026-07-31), so the real access boundary is the socket *directory*:
  `RuntimeDirectory=personal-agent` at 0770 `api:www-data` lets exactly
  Nginx's group traverse to the socket and blocks every other local user.
- **`UMask=0077` on the API unit is not cosmetic, and 0007 was wrong.**
  `Group=www-data` sets the process's *egid*, and `/var/lib/personal-agent-api`
  is not setgid, so every file the API creates — `agent.sqlite` and its `-wal` /
  `-shm` — is group `www-data` no matter what the directory is owned by. At the
  original 0007 that meant mode 0660: Nginx, the one network-facing process on
  this box, held read+write on the Agent database, and the directory's 0700 was
  the *only* thing in the way rather than the second layer. 0077 makes those
  files 0600 and breaks nothing, because the socket's mode never came from the
  umask in the first place. `verify.sh` asserts the file modes, not just the
  directory's — the directory check alone is what let this sit unnoticed.
- **`SupplementaryGroups=personal-agent-api` is what makes that survivable.**
  Setting `Group=` replaces the *primary* gid, and `useradd --system` leaves
  the group's member list in `/etc/group` empty, so `initgroups()` has nothing
  to restore it from. Without this line the service holds only www-data,
  cannot traverse `/etc/personal-agent/keys/api` (0750 root:api), and dies on
  its first key read into a restart loop. `sudo -u personal-agent-api` does
  *not* reproduce this — sudo resolves the passwd primary gid — so `verify.sh`
  asserts the group set of the real process out of `/proc`, not a sudo shell's.
- **Fresh key material is minted on the server** (`provision_server_keys.sh`),
  not copied from the development Mac. Devices enrolled against the ECS are
  bound to these rings; the Mac's rings stay the Mac's.
- **The API unit `Requires=` the MCP unit.** API composition discovers the
  Finance catalog at boot and refuses to start without it; ordering alone would
  still race the MCP's boot-time schema validation, so the dependency is
  explicit. `Restart=on-failure` absorbs the residual race.
- **`SystemCallFilter=@system-service` and `RestrictAddressFamilies` are
  shipped enabled.** §10.1 requires verifying them against the real services —
  that verification *is* this rollout (step 8/9). If a unit dies with `SECCOMP`
  in the journal, identify the syscall first, relax only what is needed, and
  record the deviation in `PROJECT_STATUS.md`. Do not preemptively weaken.
- **No `--allow-tool` is set.** The composed catalog (three write tools +
  `meta.capabilities`) is exactly what the live Simulator run exercised. A
  read-only rollout would be expressed by adding `--allow-tool` to the API
  unit's `ExecStart`; that is a product decision, not a default.

## Procedure

### 1. Ship this directory (Mac)

```sh
rsync -av --delete -e "ssh -i ~/.ssh/personal_agent_example_key" \
  deploy/ deploy@192.0.2.10:~/personal-agent-deploy/
```

### 2. Users, directories, units (ECS)

```sh
sudo bash ~/personal-agent-deploy/install.sh
```

### 3. Server key material (ECS)

```sh
sudo bash ~/personal-agent-deploy/provision_server_keys.sh
```

Creates both env skeletons with the key-ring variables. Refuses to overwrite —
key rotation is a deliberate act.

### 4. Secrets: GLM, Feishu, user id (Mac → ECS)

The local env files are mode 600 and **never printed**. Transfer with `-p` so
the 0600 survives the copy (a plain scp would land them 0644), split without
echoing, and shred the copies:

```sh
# on the Mac
scp -p -i ~/.ssh/personal_agent_example_key .env.local .env.finance.local \
  config/ledger.synthetic_test.2026.json \
  deploy@192.0.2.10:/tmp/
```

Paste this as one block. It is written as a `bash -e` script rather than as
loose lines on purpose: pasted line by line into an interactive shell, a `grep`
that matches nothing returns 1 and the next line runs anyway, so a missing key
would be discovered only when the first model call failed in production.

```sh
# on the ECS, as root
bash -e <<'PROVISION'
require() { # <variable> <source file> <destination>
  grep "^$1=" "$2" >> "$3" || {
    echo "$1 missing from $2 — append the real value to $3 by hand first" >&2
    exit 1
  }
}
require ZAI_API_KEY            /tmp/.env.local /etc/personal-agent/api.env
require PERSONAL_AGENT_USER_ID /tmp/.env.local /etc/personal-agent/api.env
# Optional: absence means the apps offer no "open the ledger" jump.
grep '^PERSONAL_AGENT_LEDGER_URL=' /tmp/.env.local >> /etc/personal-agent/api.env || true
grep '^FEISHU_FINANCE_' /tmp/.env.finance.local >> /etc/personal-agent/mcp.env
install -m 0600 -o personal-data-mcp -g personal-data-mcp \
  /tmp/ledger.synthetic_test.2026.json /var/lib/personal-data-mcp/
shred -u /tmp/.env.local /tmp/.env.finance.local /tmp/ledger.synthetic_test.2026.json
PROVISION
```

No placeholder ever goes into an env file: a made-up `PERSONAL_AGENT_USER_ID`
would pass the CLI's non-empty check and land in Finance's audit trail as a
real identity. `require` refuses rather than substituting one, and it exits the
heredoc — not the operator's SSH session.

### 5. Application code (Mac)

```sh
bash deploy/deploy_code.sh
```

Builds the wheel from the checkout, exports the locked dependency set with
hashes, installs both into `/opt/personal-agent/.venv`, and verifies both
entrypoints answer `--help`. Services are **not** restarted by this script.

### 6. Databases (ECS)

```sh
sudo -u personal-data-mcp /opt/personal-agent/.venv/bin/personal-data-mcp-db \
  --database /var/lib/personal-data-mcp/finance.sqlite upgrade
sudo -u personal-agent-api /opt/personal-agent/.venv/bin/personal-agent-db \
  --database /var/lib/personal-agent-api/agent.sqlite upgrade
```

### 7. Enable and start (ECS)

```sh
sudo systemctl enable --now personal-data-mcp personal-agent-api
sudo journalctl -u personal-data-mcp -u personal-agent-api --since -2m
```

The API refuses to start without a reachable Finance catalog, so the MCP comes
up first; `Requires=` enforces the order on every boot.

### 8. Acceptance (ECS)

```sh
sudo bash ~/personal-agent-deploy/verify.sh
```

Covers: services active as their own users, no root process, the API process's
real group set out of `/proc`, **six cross-user read refusals** (DB dirs, env
files, key dirs) **each paired with the positive control that the owning user
can read the same path**, socket group/mode for Nginx, MCP loopback-only, no
8810 TCP listener, 405 from the real MCP endpoint, 401 through the real API
socket, and `https://zhuyawei.com` still 200.

The positive controls are not decoration. `head` on a missing file and `ls` on
a missing directory both fail, so a refusal-only suite reports a deployment
that never created the secrets as fully isolated. The pair is what makes
"cannot read" mean isolation rather than absence.

### 9. Record

Append the `verify.sh` output (it contains no secrets) plus the commit hash of
the deployed wheel to `docs/evidence/`, and update `PROJECT_STATUS.md`.

## Upgrades (after first rollout)

```sh
bash deploy/deploy_code.sh          # Mac: build + install new wheel
ssh -i ~/.ssh/personal_agent_example_key deploy@192.0.2.10 \
  'sudo systemctl restart personal-data-mcp personal-agent-api'
sudo bash ~/personal-agent-deploy/verify.sh   # ECS
```

Give the services a few seconds before `verify.sh`: the API only serves after
Finance catalog discovery and boot-time schema validation, so a verify run
~3s after `restart` can report liveness/socket failures that clear on the
immediate re-run (observed 2026-08-01). Wait for `/run/personal-agent/api.sock`
to exist first, or just re-run verify before investigating.

Schema changes: run the matching `*-db upgrade` command (step 6) *before* the
restart. Every revision has a working `downgrade`.

Unit changes need `systemctl daemon-reload` before the restart — re-running
`deploy/install.sh` does both the reinstall and the reload.

**One-time, on the first rollout that carries the `UMask=0077` API unit.** Both
databases were found at **0644** on 2026-08-01 — world-readable, and
`agent.sqlite-wal` / `-shm` additionally group `www-data`, the one
network-facing process on this box. Only the 0700 directories were keeping
anyone out.

A umask alone would not have produced 0644 and cannot fix it either, for two
reasons worth writing down, because both defeat the obvious reasoning:

- **SQLite copies the main database's mode onto `-wal` and `-shm`.** It does not
  let the umask decide them. So the API's `UMask=0007` never applied to those
  files; they inherited 0644 from `agent.sqlite`, which the migration step had
  created under `sudo`'s 022. This is also why `personal-data-mcp` shows 0644
  despite having shipped `UMask=0077` from the start — the umask was never the
  operative rule.
- **A umask only shapes files created after it takes effect.** Existing ones are
  untouched by any unit change.

So the units carry `UMask=0077` for anything created fresh later (a restore, a
rebuilt database), and the existing files need a one-time chmod, as root, on
**both** services:

```sh
chmod 0600 /var/lib/personal-agent-api/* /var/lib/personal-data-mcp/*
```

Do it while the services are stopped, or restart them after: SQLite keeps the
`-wal` and `-shm` open, and the next checkpoint recreates them from the main
database's mode — which is exactly the mechanism that makes the fix stick.

`verify.sh` now asserts every file in both directories, so a missed run shows up
as a `FAIL` rather than as nothing at all.

## Operator commands on the ECS (devices, review)

```sh
/opt/personal-agent/operator-cli.sh personal-agent-device \
  --database /var/lib/personal-agent-api/agent.sqlite issue-code
/opt/personal-agent/operator-cli.sh personal-agent-device \
  --database /var/lib/personal-agent-api/agent.sqlite list
```

The wrapper runs the CLI as `personal-agent-api` with `api.env` loaded; the
0700 data directory gives `deploy` no direct path to the database, which
is what keeps the audit trail honest about which identity touched it.

## Rollback

```sh
sudo bash ~/personal-agent-deploy/rollback.sh
```

Stops/disables both units and removes them, then re-checks the personal site.
It **preserves** users, `/var/lib` data, `/etc/personal-agent` secrets and
`/opt` code — removing any of those is a separate explicit decision, not part
of a rollback.

## Not in this task (by design)

- Nginx upstream, `agent.example.invalid` DNS/TLS — **DEV-033**. The socket this
  task creates is the upstream it will proxy.
- The daily-review timer — **DEV-036** (`personal-agent-review` is installed
  but has no timer yet).
- `finance.query_expenses` — its cursor-signing credential is not loaded by
  server composition; the tool stays unadvertised (unchanged from DEV-027).
- Backups/metrics/alerts — **DEV-034/035**.
