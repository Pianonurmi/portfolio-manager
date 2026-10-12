#!/usr/bin/env bash
# Create deploy/.env with fresh secrets (once). Never commit .env.
set -euo pipefail
cd "$(dirname "$0")"
# Usage: PM_BIND=<this host's LAN address> [PM_LLM_URL=http://<llm-host>:<port>] ./init-env.sh
: "${PM_BIND:?set PM_BIND to this host's LAN address, e.g. PM_BIND=192.168.1.10 ./init-env.sh}"
if [ -f .env ]; then echo ".env exists, leaving it alone"; exit 0; fi
key=$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')
pw=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
umask 077
cat > .env <<ENV
# Generated $(date -I). Back this file up separately from the database dumps:
# without PM_MASTER_KEY, stored API tokens cannot be decrypted.
PM_MASTER_KEY=$key
POSTGRES_PASSWORD=$pw
PM_BIND=$PM_BIND
PM_PORT=8640
PM_OUTBOUND_ALLOWLIST=["data-api.ecb.europa.eu","query1.finance.yahoo.com","query2.finance.yahoo.com","lt.morningstar.com","api.ynab.com","api.kraken.com","api.binance.com","api.coingecko.com","pxdata.stat.fi","mba.tuck.dartmouth.edu","www.nordnet.fi"]
# LAN services the app may call (Gotify, Paperless, a local AI gateway), as JSON: ["192.168.1.20","gotify.lan"]
PM_LAN_ALLOWLIST=[]
# OpenAI-compatible local LLM (optional); its host must also be in PM_LAN_ALLOWLIST
PM_LLM_URL=${PM_LLM_URL:-}
ENV
echo "wrote deploy/.env"
