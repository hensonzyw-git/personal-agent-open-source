# DEV-033 — Nginx/TLS/rate limit for agent.example.invalid

> Status: **applied to the ECS on 2026-07-31**; evidence in
> `docs/evidence/DEV032_DEV033_部署验收_2026-07-31.md`.
> Prerequisite: DEV-032 rolled out (the Unix socket exists and both services
> are active), `agent.example.invalid` A record → `192.0.2.10` (done 2026-07-31).
> Acceptance (Phase1 拆解 Wave 5): MCP/DB 无公网端口；未知 Host 拒绝；证书续期告警。
> Design: `docs/Phase1技术方案_v0.1.md` §10.3.

## What this lays down

| File | Installed to | Purpose |
|---|---|---|
| `agent.example.invalid.conf` | `sites-available/agent.example.invalid` → symlink | port-80 vhost, rate zones, query-free log format, Origin check |
| `agent.example.invalid-ssl.conf` | `sites-available/agent.example.invalid-ssl` → symlink, **after issuance** | the TLS vhost; references cert files that must already exist |
| `pa-upstream.conf` | `/etc/nginx/snippets/pa-upstream.conf` | the one UDS upstream, shared by every location |
| `default-deny-443.conf` | `sites-available/default-deny-443` → symlink | `ssl_reject_handshake` for unknown Host on 443 |
| `cert-expiry-check.sh` | `/usr/local/lib/personal-agent/` | daily expiry alarm (journal + non-zero exit) |
| `personal-agent-cert-check.{service,timer}` | `/etc/systemd/system/` | runs the check daily |

Decisions and why:

- **No HTTP→HTTPS redirect for the agent vhost.** Port 80 answers 444. The
  only client is the iOS app, which only ever speaks TLS; a redirect exists
  for browsers, and this API has none.
- **The log format drops the query string.** History cursors travel in the
  query; the stock combined format would write them into
  `/var/log/nginx`. `Authorization` is a header and no stock format logs it.
- **Origin is rejected when present.** A native app sends no `Origin`; a
  browser does. This is a fail-closed default, not a CORS policy.
- **`certbot certonly --nginx`, not the installer.** The existing site cert
  uses the nginx installer, which edits vhosts itself; this vhost is
  hand-written and reviewed, so certbot is used for the challenge only and
  renewal gets a deploy hook that reloads Nginx.
- **The expiry alarm's channel is DEV-034's.** This task's contract is that an
  impending expiry cannot pass silently: journal `crit` plus a failed unit.
  Let's Encrypt's own expiry email to the account address is the backstop.
- **The alarm checks the served certificate, not only the files.** Reading
  `/etc/letsencrypt/live/*/cert.pem` alone answers "did renewal run", never
  "is the renewed certificate the one clients get". Those diverge whenever the
  deploy hook below stops reloading Nginx — dropped by a certbot reinstall,
  failing quietly, or reloading an Nginx that then declines to start — and in
  that state the file dates look perfect while the iPhone app fails its TLS
  handshake and stops working entirely. So the check also dials
  `agent.example.invalid:443` and `zhuyawei.com:443` and reads the leaf it is
  actually handed. It follows that the alarm needs network egress and will
  (correctly) fire when the host is unreachable.

## Procedure

### 1. Ship and back up (Mac)

```sh
rsync -av -e "ssh -i ~/.ssh/personal_agent_example_key" deploy/nginx/ \
  deploy@192.0.2.10:~/personal-agent-deploy/nginx/
```

### 2. Back up the live Nginx config (ECS)

```sh
sudo cp -a /etc/nginx /etc/nginx.pre-dev033.$(date +%Y%m%d)
```

### 3. Install the port-80 vhost and issue the certificate (ECS)

```sh
sudo install -m 0644 ~/personal-agent-deploy/nginx/agent.example.invalid.conf \
  /etc/nginx/sites-available/agent.example.invalid
sudo ln -s /etc/nginx/sites-available/agent.example.invalid \
  /etc/nginx/sites-enabled/agent.example.invalid
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --nginx -d agent.example.invalid
```

### 4. Install the TLS vhost and the 443 default deny (ECS)

The ssl conf references the certificate from step 3, so it can only go in
after issuance (`nginx -t` would fail on the missing files otherwise).

```sh
sudo install -m 0644 ~/personal-agent-deploy/nginx/pa-upstream.conf \
  /etc/nginx/snippets/pa-upstream.conf
sudo install -m 0644 ~/personal-agent-deploy/nginx/agent.example.invalid-ssl.conf \
  /etc/nginx/sites-available/agent.example.invalid-ssl
sudo install -m 0644 ~/personal-agent-deploy/nginx/default-deny-443.conf \
  /etc/nginx/sites-available/default-deny-443
sudo ln -s /etc/nginx/sites-available/agent.example.invalid-ssl \
  /etc/nginx/sites-enabled/agent.example.invalid-ssl
sudo ln -s /etc/nginx/sites-available/default-deny-443 \
  /etc/nginx/sites-enabled/default-deny-443
sudo nginx -t && sudo systemctl reload nginx
```

### 5. Renewal hook and expiry alarm (ECS)

```sh
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy /usr/local/lib/personal-agent
printf '#!/bin/sh\nsystemctl reload nginx\n' | \
  sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo install -m 0755 ~/personal-agent-deploy/nginx/cert-expiry-check.sh \
  /usr/local/lib/personal-agent/
sudo install -m 0644 ~/personal-agent-deploy/nginx/personal-agent-cert-check.service \
  ~/personal-agent-deploy/nginx/personal-agent-cert-check.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now personal-agent-cert-check.timer
sudo systemctl start personal-agent-cert-check.service   # immediate self-test
sudo certbot renew --dry-run
```

### 6. Acceptance (Mac is fine)

```sh
curl -sI https://zhuyawei.com | head -1                       # personal site: 200
curl -sI https://www.zhuyawei.com | head -1                   # www redirect intact
curl -sI https://agent.example.invalid/v1/capabilities           # 401, over the UDS
curl -sI -H 'Origin: https://evil.example' \
  https://agent.example.invalid/v1/capabilities                  # 403
curl -skI --resolve unknown.zhuyawei.com:443:192.0.2.10 \
  https://unknown.zhuyawei.com/                               # handshake refused
curl -sI http://agent.example.invalid/                           # 444 (empty reply)
ss -tlnp | grep -E '8810|8811'                                # must print nothing public
```

Rate-limit spot check: burst `/v1/capabilities` past the zone and expect 429s.
Log check: `sudo tail /var/log/nginx/agent.example.invalid.access.log` must show
no `?cursor=` and no `Authorization`.

## Rollback

```sh
sudo rm /etc/nginx/sites-enabled/agent.example.invalid \
        /etc/nginx/sites-enabled/agent.example.invalid-ssl \
        /etc/nginx/sites-enabled/default-deny-443
sudo systemctl disable --now personal-agent-cert-check.timer
sudo nginx -t && sudo systemctl reload nginx
```

The backup from step 2 restores anything beyond the two symlinks. Certificate
material under `/etc/letsencrypt` is preserved; deleting it is a separate
explicit decision.
