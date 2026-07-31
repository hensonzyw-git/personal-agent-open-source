#!/usr/bin/env bash
# DEV-033: certificate expiry alarm.
#
# certbot.timer renews at <30 days; this check is the alarm half of the design
# 10.3 requirement ("TLS 证书自动续期并有到期告警"). It fails loudly — journal
# priority crit and a non-zero exit — when any certificate has fewer than
# WARN_DAYS days left, which means renewal has already had ~two weeks to
# succeed and has not.
#
# Two sources, deliberately, because they fail in different ways:
#
#   1. the files under /etc/letsencrypt/live — catches "renewal never ran".
#   2. what the server is *actually* serving on 443 — catches "renewal ran but
#      Nginx was never reloaded", which source 1 reports as perfectly healthy
#      while the app hits an expired certificate and stops working entirely.
#      The runbook installs the deploy hook that reloads Nginx
#      (/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh, README step 5),
#      so this is not a gap in the design — it is the check that the design is
#      still true on the box. A hook can be dropped by a certbot reinstall, fail
#      silently, or reload an Nginx that then declines to start; in every one of
#      those the file dates look fine and only the served certificate is wrong.
#
# The alert *channel* (journal -> wherever DEV-034 sends alerts) is DEV-034's;
# this script's contract is only that an impending expiry is impossible to
# miss in the journal and in `systemctl status personal-agent-cert-check`.

set -uo pipefail
# Without this an empty /etc/letsencrypt/live hands the literal glob to openssl,
# and the resulting failure inside the loop pre-empts the "no certificates
# found" alarm this script exists to raise.
shopt -s nullglob

WARN_DAYS=14
# The names the iPhone app and the personal site actually depend on. A
# certificate that is fresh on disk but not being served is the whole reason
# this list exists.
SERVED_NAMES=(agent.example.invalid zhuyawei.com)

now=$(date +%s)
alarms=0
worst_name=""
worst_days=""

days_left() { # PEM on stdin -> days remaining, or non-zero when unreadable
    local end end_ts
    end=$(openssl x509 -enddate -noout 2>/dev/null | cut -d= -f2)
    [ -n "$end" ] || return 1
    end_ts=$(date -d "$end" +%s 2>/dev/null) || return 1
    echo $(( (end_ts - now) / 86400 ))
}

note() { # <label> <days>
    local label="$1" days="$2"
    if [ -z "$worst_days" ] || [ "$days" -lt "$worst_days" ]; then
        worst_name="$label"
        worst_days="$days"
    fi
    if [ "$days" -lt "$WARN_DAYS" ]; then
        echo "CERT EXPIRY ALARM: $label expires in $days day(s) (< $WARN_DAYS)" >&2
        alarms=$((alarms + 1))
    fi
}

# --- 1. on disk ---------------------------------------------------------------
found=0
for cert in /etc/letsencrypt/live/*/cert.pem; do
    found=1
    name=$(basename "$(dirname "$cert")")
    if ! days=$(days_left < "$cert"); then
        echo "CERT EXPIRY ALARM: cannot read the certificate at $cert" >&2
        alarms=$((alarms + 1))
        continue
    fi
    note "$name (on disk)" "$days"
done
if [ "$found" -eq 0 ]; then
    echo "CERT EXPIRY ALARM: no certificates found under /etc/letsencrypt/live" >&2
    alarms=$((alarms + 1))
fi

# --- 2. what is actually served ----------------------------------------------
for host in "${SERVED_NAMES[@]}"; do
    # `timeout` because s_client will sit on a black-holed port until systemd's
    # own start timeout kills the unit, which reports as a confusing failure
    # rather than as this check's own alarm.
    served=$(timeout 15 openssl s_client -connect "$host:443" \
        -servername "$host" </dev/null 2>/dev/null | openssl x509 2>/dev/null)
    if [ -z "$served" ]; then
        echo "CERT EXPIRY ALARM: $host served no readable certificate on 443" >&2
        alarms=$((alarms + 1))
        continue
    fi
    if ! days=$(printf '%s\n' "$served" | days_left); then
        echo "CERT EXPIRY ALARM: $host served an unparseable certificate" >&2
        alarms=$((alarms + 1))
        continue
    fi
    note "$host (served)" "$days"
done

if [ "$alarms" -ne 0 ]; then
    exit 1
fi

echo "cert expiry OK: nearest is $worst_name in $worst_days day(s)"
