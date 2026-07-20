#!/usr/bin/env bash
# Auto-reconnecting SSH tunnel: expose the loopback-only ollama server on
# muscat-ut4 as a local port on this host (muscat-ut2), so the chat assistant
# (src/muscat_db/chat_agent.py) can reach it via MUSCAT_OLLAMA_URL.
#
# ollama on muscat-ut4 binds 127.0.0.1:11434 (no auth), so we do NOT expose it
# on the network. Instead we forward a local port through SSH; ollama stays
# loopback-only and only users who can SSH to ut4 can reach it.
#
# Point the app at the local end:  MUSCAT_OLLAMA_URL=http://127.0.0.1:11434
#
# Run under a dedicated tmux session so it survives and auto-reconnects:
#     tmux new-session -d -s ollama-tunnel 'scripts/ollama_tunnel.sh'
#
# Override any of these via env:
#     LOCAL_PORT=11434 REMOTE_HOST=muscat-ut4 REMOTE_BIND=127.0.0.1 \
#     REMOTE_PORT=11434 RETRY_DELAY_S=5 scripts/ollama_tunnel.sh
set -euo pipefail

LOCAL_PORT="${LOCAL_PORT:-11434}"
REMOTE_HOST="${REMOTE_HOST:-muscat-ut4}"
REMOTE_BIND="${REMOTE_BIND:-127.0.0.1}"   # interface ollama listens on, from ut4's view
REMOTE_PORT="${REMOTE_PORT:-11434}"
RETRY_DELAY_S="${RETRY_DELAY_S:-5}"

echo "[ollama-tunnel] forwarding localhost:${LOCAL_PORT} -> ${REMOTE_HOST}:${REMOTE_BIND}:${REMOTE_PORT}"

while true; do
  # -N: no remote command; ExitOnForwardFailure: bail if the local port can't
  # bind (e.g. already in use) instead of a silently-dead forward; ServerAlive*:
  # drop and reconnect within ~90s of a network stall; BatchMode: never prompt.
  ssh -N \
      -o BatchMode=yes \
      -o ExitOnForwardFailure=yes \
      -o ConnectTimeout=10 \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -L "${LOCAL_PORT}:${REMOTE_BIND}:${REMOTE_PORT}" \
      "${REMOTE_HOST}" \
    || echo "[ollama-tunnel] ssh exited ($?), reconnecting in ${RETRY_DELAY_S}s"
  sleep "${RETRY_DELAY_S}"
done
