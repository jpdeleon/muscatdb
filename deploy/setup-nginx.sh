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
