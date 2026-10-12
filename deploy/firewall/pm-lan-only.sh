#!/usr/bin/env bash
# Restrict the Portfolio Manager port to private networks.
# Docker-published ports bypass the INPUT chain, so the rule lives in DOCKER-USER and matches
# the original (pre-DNAT) destination. Idempotent: removes its own rules, then re-adds them.
# Only touches rules tagged "pm-lan-only"; other apps on this host are unaffected.
# Phase 0 moves this into Ansible and makes it persistent across reboots.
set -euo pipefail
PORTS="${PM_PORTS:-8640}"
ALLOWED="${PM_ALLOWED_CIDRS:-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16}"
TAG="pm-lan-only"
IPT="${IPT:-iptables}"

# remove existing tagged rules
while read -r rule; do
  [ -n "$rule" ] || continue
  eval "$IPT ${rule/-A /-D }"
done < <($IPT -S DOCKER-USER | grep -- "--comment $TAG" || true)

for port in $PORTS; do
  # inserted in reverse order: DROP ends up last, RETURNs above it
  $IPT -I DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -m comment --comment "$TAG" -j DROP
  for cidr in $ALLOWED; do
    $IPT -I DOCKER-USER -s "$cidr" -p tcp -m conntrack --ctorigdstport "$port" -m comment --comment "$TAG" -j RETURN
  done
done
# worker's mail network: SMTP to private addresses only, nothing else leaves it
MAIL="${PM_SUBNET_MAIL:-172.30.89.0/24}"
$IPT -I DOCKER-USER -s "$MAIL" -m comment --comment "$TAG" -j DROP
$IPT -I DOCKER-USER -s "$MAIL" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$TAG" -j RETURN
for cidr in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
  $IPT -I DOCKER-USER -s "$MAIL" -d "$cidr" -p tcp -m multiport --dports 25,465,587 -m comment --comment "$TAG" -j RETURN
done
$IPT -I DOCKER-USER -s "$MAIL" -d "$MAIL" -m comment --comment "$TAG" -j RETURN
$IPT -S DOCKER-USER | grep -- "$TAG"
