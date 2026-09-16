#!/bin/bash
# G2 isolation canary — read-only capability probe for the pending decoder sandbox.
#
# Evidence tooling, NOT a production template (AGENTS.md §5.1). Companion to
# docs/evidence/G2a_隔离能力探测_2026-09-10.md; the plan is
# docs/多模态输入G2-G4验证方案_v0.1.md §1.
#
# Run from a machine that can ssh to the ECS, as root on the far side:
#     ssh personal-agent-ecs 'sudo bash -s' < scripts/g2_isolation_canary.sh
#
# It reads NO credential contents: file reachability is decided by `test -r`
# alone, and the memory canary allocates anonymous pages only. Everything it
# starts is an ephemeral systemd unit with --collect, so it cleans itself up.
set -u

API_USER=personal-agent-api
API_GROUP=personal-agent-api

echo "== platform =="
head -3 /etc/os-release
uname -r
systemctl --version | head -1

echo
echo "== target unit =="
systemctl show personal-agent-api.service \
  -p User -p Group -p SupplementaryGroups -p EnvironmentFiles -p FragmentPath -p DropInPaths
id "$API_USER"

echo
echo "== running process identity =="
p=$(systemctl show personal-agent-api.service -p MainPID --value)
echo "MainPID=$p"
grep -E "^(Uid|Gid|CapEff|NoNewPrivs):" "/proc/$p/status"

echo
echo "== P1a: transient unit as the API identity =="
systemd-run --uid="$API_USER" --gid="$API_GROUP" --pipe --wait --collect /usr/bin/id 2>&1 \
  || echo "P1a_FAILED"

echo
echo "== P1b: unprivileged user namespace (expect DENIED on Ubuntu 24.04) =="
sysctl kernel.unprivileged_userns_clone
sysctl kernel.apparmor_restrict_unprivileged_userns
sudo -u "$API_USER" unshare -U -r /usr/bin/id 2>&1 || echo "P1b_USERNS_DENIED"
echo "-- why (kernel's own words, not an inference) --"
dmesg 2>/dev/null | grep -i apparmor | tail -3 || echo "no_apparmor_lines_visible"

echo
echo "== P1c: do the intended hardening directives actually bite? =="
systemd-run --uid="$API_USER" --gid="$API_GROUP" \
  -p RestrictAddressFamilies=AF_UNIX \
  -p NoNewPrivileges=yes \
  -p PrivateTmp=yes \
  -p ProtectSystem=strict \
  -p MemoryMax=64M \
  -p TasksMax=16 \
  --pipe --wait --collect /bin/bash -c '
    echo "  actual uid=$(id -u) gid=$(id -g) caps=$(grep CapEff /proc/self/status | cut -f2)"
    (exec 3<>/dev/tcp/1.1.1.1/80) 2>/dev/null && echo "  INET_OK" || echo "  INET_DENIED"
    echo "  NoNewPrivs=$(grep NoNewPrivs /proc/self/status | cut -f2)"
  ' 2>&1 || echo "P1c_FAILED"

echo
echo "== P1d: polkit surface for an unprivileged unit start =="
systemctl is-active polkit 2>&1 || true
ls -A /etc/polkit-1/rules.d/ 2>&1 | grep -q . && ls /etc/polkit-1/rules.d/ || echo "no_local_polkit_rules"

echo
echo "== P2: does a decoder identity already exist? =="
for u in personal-agent-decoder personal-agent-media personal-agent-sandbox; do
  getent passwd "$u" >/dev/null 2>&1 && echo "$u EXISTS" || echo "$u absent"
done
echo "-- groups the decoder identity must NOT join --"
getent group personal-agent-api www-data personal-data-mcp personal-agent-dal personal-agent-backup

echo
echo "== P3(ii): credential reachability (reachability only, contents never read) =="
for f in api.env backup.env restic.env mcp.env dal.env annual-readonly.env; do
  if sudo -u "$API_USER" test -r "/etc/personal-agent/$f"; then
    echo "$f READABLE_BY_API"
  else
    echo "$f DENIED"
  fi
done
echo "-- keys tree (modes only) --"
find /etc/personal-agent/keys -maxdepth 2 -printf "%M %u:%g %p\n" 2>/dev/null | head -20

echo
echo "== P6: data directories =="
ls -ld /var/lib/personal-agent* 2>&1

echo
echo "== P7: does MemoryMax kill rather than degrade? =="
echo "-- control, no limit --"
systemd-run --uid="$API_USER" --gid="$API_GROUP" \
  --pipe --wait --collect \
  /usr/bin/python3 -c 'b = bytearray(200*1024*1024); print("ALLOCATED_200M_ANYWAY")' 2>&1
echo "control exit=$?"
echo "-- test, MemoryMax=32M (expect oom-kill, not degradation) --"
systemd-run --uid="$API_USER" --gid="$API_GROUP" \
  -p MemoryMax=32M --pipe --wait --collect \
  /usr/bin/python3 -c 'b = bytearray(200*1024*1024); print("ALLOCATED_200M_ANYWAY")' 2>&1
echo "test exit=$?"

echo
echo "== NOT COVERED HERE (G2b — needs the real launcher) =="
echo "  P3(i) environment inheritance, P5 inherited fds, P8 fail-closed on launcher failure"
