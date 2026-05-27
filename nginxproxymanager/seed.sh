#!/bin/bash
# Idempotent NPM bootstrap: admin user, proxy hosts, wildcard cert.
# Re-running this script with no state change produces no API writes.

set -e

SCRIPT_DIR="$(realpath "$(dirname "$0")")"
REPO_DIR="$(realpath "$SCRIPT_DIR/..")"

# shellcheck disable=SC1091
source "$REPO_DIR/config.conf"
# shellcheck disable=SC1091
[ -f "$REPO_DIR/config.local.conf" ] && source "$REPO_DIR/config.local.conf"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

# Validate required settings
[ -n "${NPM_ADMIN_EMAIL:-}" ]    || npm_die "NPM_ADMIN_EMAIL not set in config.local.conf"
[ -n "${NPM_ADMIN_PASSWORD:-}" ] || npm_die "NPM_ADMIN_PASSWORD not set in config.local.conf"
[ -n "${SERVER_LAN_IP:-}" ]      || npm_die "SERVER_LAN_IP not set in config.conf"
[ -n "${DUCKDNS_DOMAIN:-}" ]     || npm_die "DUCKDNS_DOMAIN not set in config.conf"
[ -n "${NPM_PROXY_HOSTS:-}" ]    || npm_die "NPM_PROXY_HOSTS not set in config.conf"

WILDCARD="*.${DUCKDNS_DOMAIN}.duckdns.org"

npm_wait_for_api
npm_login "$NPM_ADMIN_EMAIL" "$NPM_ADMIN_PASSWORD"

###############################################################################
# 1. Wildcard certificate via DuckDNS DNS-01

CERT_ID=$(npm_find_cert_id "$WILDCARD")

if [ -n "$CERT_ID" ]; then
	npm_log "Wildcard cert $WILDCARD already present (id=$CERT_ID)"
else
	[ -n "${DUCKDNS_TOKEN:-}" ]    || npm_die "DUCKDNS_TOKEN not set; cannot request wildcard cert"
	[ -n "${LETSENCRYPT_EMAIL:-}" ] || npm_die "LETSENCRYPT_EMAIL not set in config.conf"

	npm_log "Requesting wildcard cert $WILDCARD (this can take ~1–2 min)"
	body=$(jq -nc \
		--arg domain "$WILDCARD" \
		--arg email "$LETSENCRYPT_EMAIL" \
		--arg creds "dns_duckdns_token = $DUCKDNS_TOKEN" \
		'{provider:"letsencrypt", domain_names:[$domain], meta:{
			letsencrypt_email:$email,
			letsencrypt_agree:true,
			dns_challenge:true,
			dns_provider:"duckdns",
			dns_provider_credentials:$creds,
			propagation_seconds:60
		}}')
	resp=$(npm_post "nginx/certificates" "$body")
	CERT_ID=$(printf '%s' "$resp" | jq -r '.id')
	[ -n "$CERT_ID" ] && [ "$CERT_ID" != "null" ] || npm_die "Cert creation failed: $resp"
	npm_log "Created wildcard cert id=$CERT_ID"
fi

###############################################################################
# 2. Proxy hosts

for spec in $NPM_PROXY_HOSTS; do
	sub="${spec%%:*}"
	port="${spec##*:}"
	domain="${sub}.${DUCKDNS_DOMAIN}.duckdns.org"

	host_id=$(npm_find_proxy_host_id "$domain")
	if [ -n "$host_id" ]; then
		npm_log "Proxy host $domain already exists (id=$host_id)"
		continue
	fi

	body=$(jq -nc \
		--arg dom "$domain" \
		--arg host "$SERVER_LAN_IP" \
		--argjson port "$port" \
		--argjson cert "$CERT_ID" \
		'{
			domain_names:[$dom],
			forward_scheme:"http",
			forward_host:$host,
			forward_port:$port,
			access_list_id:0,
			certificate_id:$cert,
			ssl_forced:true,
			http2_support:true,
			hsts_enabled:true,
			meta:{letsencrypt_agree:false, dns_challenge:false},
			advanced_config:"",
			locations:[],
			block_exploits:false,
			caching_enabled:false,
			allow_websocket_upgrade:true
		}')
	resp=$(npm_post "nginx/proxy-hosts" "$body")
	host_id=$(printf '%s' "$resp" | jq -r '.id')
	[ -n "$host_id" ] && [ "$host_id" != "null" ] || npm_die "Proxy host creation for $domain failed: $resp"
	npm_log "Created proxy host $domain (id=$host_id)"
done

npm_log "Seed complete."
