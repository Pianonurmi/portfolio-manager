#!/bin/sh
# Build the tinyproxy host filter from PM_OUTBOUND_ALLOWLIST (JSON list or comma-separated).
set -eu
list=$(echo "${PM_OUTBOUND_ALLOWLIST:-},${PM_LAN_ALLOWLIST:-}" | tr -d '[]" ' | tr ',' '\n' | sed '/^$/d')
: > /etc/tinyproxy/filter
for h in $list; do
  esc=$(echo "$h" | sed 's/\./\\./g')
  echo "^(.*\.)?${esc}\$" >> /etc/tinyproxy/filter
done
cat > /etc/tinyproxy/tinyproxy.conf <<CONF
User nobody
Group nobody
Port 3128
Timeout 60
LogLevel Notice
MaxClients 50
Allow 172.16.0.0/12
Allow 10.0.0.0/8
Allow 192.168.0.0/16
ConnectPort 443
# plain HTTP is proxied too, but only for hosts in the filter (LAN services such as Gotify)
Filter "/etc/tinyproxy/filter"
FilterExtended Yes
FilterDefaultDeny Yes
DisableViaHeader Yes
CONF
echo "egress allowlist:"; cat /etc/tinyproxy/filter
exec tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf
