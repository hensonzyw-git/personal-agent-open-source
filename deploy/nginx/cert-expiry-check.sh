#!/usr/bin/env bash
# DEV-033: certificate expiry alarm.
#
# certbot.timer renews at <30 days; this check is the alarm half of the design
# 10.3 requirement ("TLS 证书自动续期并有到期告警"). It fails loudly — journal
# priority crit and a non-zero exit — when any live certificate has fewer than
# WARN_DAYS days left, which means renewal has already had ~two weeks to
# succeed and has not.
#
# The alert *channel* (journal -> wherever DEV-034 sends alerts) is DEV-034's;
# this script's contract is only that an impending expiry is impossible to
# miss in the journal and in `systemctl status personal-agent-cert-check`.

set -euo pipefail

WARN_DAYS=14
now=$(date +%s)
worst_name=""
worst_days=""

for cert in /etc/letsencrypt/live/*/cert.pem; do
    name=$(basename "$(dirname "$cert")")
    end=$(openssl x509 -enddate -noout -in "$cert" | cut -d= -f2)
    end_ts=$(date -d "$end" +%s)
    days=$(( (end_ts - now) / 86400 ))
    if [ -z "$worst_days" ] || [ "$days" -lt "$worst_days" ]; then
        worst_name=$name
        worst_days=$days
    fi
    if [ "$days" -lt "$WARN_DAYS" ]; then
        echo "CERT EXPIRY ALARM: $name expires in $days day(s) (< $WARN_DAYS): $end" >&2
    fi
done

if [ -z "$worst_days" ]; then
    echo "CERT EXPIRY ALARM: no certificates found under /etc/letsencrypt/live" >&2
    exit 1
fi

# Non-zero when anything printed above; systemd records the unit as failed.
for cert in /etc/letsencrypt/live/*/cert.pem; do
    name=$(basename "$(dirname "$cert")")
    end=$(openssl x509 -enddate -noout -in "$cert" | cut -d= -f2)
    end_ts=$(date -d "$end" +%s)
    days=$(( (end_ts - now) / 86400 ))
    if [ "$days" -lt "$WARN_DAYS" ]; then
        exit 1
    fi
done

echo "cert expiry OK: nearest is $worst_name in $worst_days day(s)"
