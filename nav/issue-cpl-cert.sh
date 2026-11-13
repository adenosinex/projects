#!/bin/bash
# Issue LE cert for cpl.zin7.de via acme.sh DNS-01 (Cloudflare).
# Reads credentials from cf-zin7.env at runtime; never prints them.
set -e
ENV_FILE=/Users/zin/mac-compute/cf-zin7.env
CF_Token=$(grep '^CF_API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
CF_Zone_ID=$(grep '^CF_ZONE_ID=' "$ENV_FILE" | cut -d= -f2-)
export CF_Token CF_Zone_ID
export CF_Account_ID=""
/opt/homebrew/bin/acme.sh --issue --dns dns_cf -d cpl.zin7.de --keylength ec-256 \
  --cert-file /Users/zin/mac-compute/certs/cpl.zin7.de.crt \
  --key-file /Users/zin/mac-compute/certs/cpl.zin7.de.key \
  --fullchain-file /Users/zin/mac-compute/certs/cpl.zin7.de.fullchain.crt
echo "cert issued; files:"
ls -la /Users/zin/mac-compute/certs/cpl.zin7.de.*
