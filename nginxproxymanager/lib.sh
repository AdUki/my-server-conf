# Small library of helpers for talking to the Nginx Proxy Manager HTTP API.
# Source this file from seed.sh; do not run it directly.
#
# Globals set:
#   NPM_BASE   — base URL of the API (default http://localhost:81/api)
#   NPM_TOKEN  — bearer token after npm_login

NPM_BASE="${NPM_BASE:-http://localhost:81/api}"
NPM_TOKEN=""

npm_log()  { echo "[npm-seed] $*" >&2; }
npm_warn() { echo "[npm-seed] WARN: $*" >&2; }
npm_die()  { echo "[npm-seed] ERROR: $*" >&2; exit 1; }

# Wait for the NPM API to become reachable. Timeout ~5 minutes.
npm_wait_for_api() {
	local i
	for i in $(seq 1 150); do
		if curl -fsS -m 3 "$NPM_BASE/" >/dev/null 2>&1; then
			return 0
		fi
		sleep 2
	done
	npm_die "NPM API did not become reachable at $NPM_BASE within 5 minutes"
}

# Attempt to authenticate. Sets NPM_TOKEN on success, returns 1 on failure.
npm_try_login() {
	local identity="$1" secret="$2"
	local resp token
	resp=$(curl -fsS -m 10 -X POST "$NPM_BASE/tokens" \
		-H 'Content-Type: application/json' \
		-d "$(jq -nc --arg i "$identity" --arg s "$secret" '{identity:$i, secret:$s}')" 2>/dev/null) || return 1
	token=$(printf '%s' "$resp" | jq -r '.token // empty')
	[ -z "$token" ] && return 1
	NPM_TOKEN="$token"
	return 0
}

# Bootstrap login: prefer configured creds; if that fails, try the upstream
# defaults (admin@example.com / changeme) and rotate the account afterwards.
npm_login() {
	local target_email="$1" target_password="$2"

	if npm_try_login "$target_email" "$target_password"; then
		npm_log "Logged in as $target_email"
		return 0
	fi

	npm_log "Login with configured credentials failed; trying upstream defaults"
	if ! npm_try_login "admin@example.com" "changeme"; then
		npm_die "Cannot log in with configured credentials nor with the upstream default. Reset NPM data or fix credentials in config.local.conf."
	fi

	npm_log "Logged in with default credentials; rotating to $target_email"
	npm_rotate_admin "$target_email" "$target_password"
	npm_try_login "$target_email" "$target_password" || \
		npm_die "Re-login with rotated credentials failed"
}

# Rotate the default admin: change email/name first, then password.
npm_rotate_admin() {
	local new_email="$1" new_password="$2"

	npm_put "users/1" "$(jq -nc \
		--arg email "$new_email" \
		'{name:"Administrator", nickname:"Admin", email:$email, roles:["admin"], is_disabled:false}')" >/dev/null

	npm_post "users/1/auth" "$(jq -nc \
		--arg cur "changeme" \
		--arg new "$new_password" \
		'{type:"password", current:$cur, secret:$new}')" >/dev/null
}

npm_get() {
	local path="$1"
	curl -fsS -m 10 -H "Authorization: Bearer $NPM_TOKEN" "$NPM_BASE/$path"
}

npm_post() {
	local path="$1" body="$2"
	curl -fsS -m 30 -X POST "$NPM_BASE/$path" \
		-H "Authorization: Bearer $NPM_TOKEN" \
		-H 'Content-Type: application/json' \
		-d "$body"
}

npm_put() {
	local path="$1" body="$2"
	curl -fsS -m 30 -X PUT "$NPM_BASE/$path" \
		-H "Authorization: Bearer $NPM_TOKEN" \
		-H 'Content-Type: application/json' \
		-d "$body"
}

# Return the certificate id (number) for an existing Let's Encrypt cert whose
# domain_names contains a given pattern, or empty if none.
npm_find_cert_id() {
	local pattern="$1"
	npm_get "nginx/certificates" \
		| jq -r --arg p "$pattern" '.[] | select(.provider == "letsencrypt") | select(.domain_names | any(. == $p)) | .id' \
		| head -n1
}

# Return the proxy-host id for a domain, or empty if none.
npm_find_proxy_host_id() {
	local domain="$1"
	npm_get "nginx/proxy-hosts" \
		| jq -r --arg d "$domain" '.[] | select(.domain_names | any(. == $d)) | .id' \
		| head -n1
}
