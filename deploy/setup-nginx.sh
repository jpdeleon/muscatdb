#!/usr/bin/env bash
# First-time nginx setup for muscat-db.
# Run as root (sudo).
#
# Usage:  sudo bash deploy/setup-nginx.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NGINX_CONF_SRC="$REPO_DIR/deploy/nginx.conf"
NGINX_CONF_DST="/etc/nginx/sites-available/muscat-db"
HTPASSWD_PATH="/etc/nginx/.htpasswd-muscatdb"
PROXY_SECRET_PATH="/etc/muscat-db/proxy-secret"
PROXY_SECRET_CONF="/etc/nginx/muscat-db-proxy-secret.conf"

echo "==> Installing nginx..."
apt-get update -qq
apt-get install -y -qq nginx openssl

echo "==> Enabling sites-available directory..."
mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled

echo "==> Installing muscat-db nginx config..."
cp "$NGINX_CONF_SRC" "$NGINX_CONF_DST"

# Enable the site (remove default if it conflicts)
if [ -f /etc/nginx/sites-enabled/default ]; then
    rm -f /etc/nginx/sites-enabled/default
fi
if [ ! -f /etc/nginx/sites-enabled/muscat-db ]; then
    ln -sf "$NGINX_CONF_DST" /etc/nginx/sites-enabled/
fi

# Create empty htpasswd file, readable only by root and nginx's worker group
# (www-data on Debian/Ubuntu). It holds password hashes for every user, so
# it must not be world-readable on a shared server.
touch "$HTPASSWD_PATH"
chown root:www-data "$HTPASSWD_PATH"
chmod 640 "$HTPASSWD_PATH"

# Authenticate the reverse proxy to uvicorn.  The application reads the raw
# secret while nginx includes only the generated proxy_set_header directive.
mkdir -p /etc/muscat-db
if [ ! -s "$PROXY_SECRET_PATH" ]; then
    openssl rand -hex 32 > "$PROXY_SECRET_PATH"
fi
APP_USER="$(stat -c '%U' "$REPO_DIR")"
# The uvicorn account needs the raw secret; no other local account does.  Do
# not grant the repository's (potentially shared) group read access, otherwise
# a group member could impersonate nginx on the loopback uvicorn port.
chown "$APP_USER":root "$PROXY_SECRET_PATH"
chmod 600 "$PROXY_SECRET_PATH"
PROXY_SECRET="$(tr -d '\r\n' < "$PROXY_SECRET_PATH")"
printf 'proxy_set_header X-MuSCAT-Proxy-Secret "%s";\n' "$PROXY_SECRET" > "$PROXY_SECRET_CONF"
chown root:root "$PROXY_SECRET_CONF"
chmod 600 "$PROXY_SECRET_CONF"
unset PROXY_SECRET

# Test config
echo "==> Testing nginx configuration..."
nginx -t

# Reload nginx
echo "==> Reloading nginx..."
systemctl reload nginx || systemctl restart nginx

echo ""
echo "==> nginx is ready on port 8000 (127.0.0.1 only)."
echo "    Next steps:"
echo "      1. Add users:  uv run muscat-db htpasswd add <username>"
echo "      2. Restart the muscat-db server with --nginx:"
echo "         uv run muscat-db restart --nginx"
echo ""
echo "    Users connect: ssh -L 8000:localhost:8000 user@server"
echo "    Then browse to http://localhost:8000"
