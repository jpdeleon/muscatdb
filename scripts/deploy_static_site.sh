#!/usr/bin/env bash
# Build the static documentation snapshot on the host (where muscat.db and the
# ~/ql photometry/timer figure trees live) and publish it to GitHub Pages
# WITHOUT committing the figures to main/test history.
#
# The built tree is force-pushed as a single *orphan* commit onto the `pages`
# branch, and .github/workflows/pages.yml deploys that branch. Because the
# branch is always force-pushed (never appended to), regenerated binary figures
# do not accumulate in the repository's history — each publish replaces the
# previous snapshot.
#
# Usage:
#   scripts/deploy_static_site.sh                 # build + publish to origin/pages
#   scripts/deploy_static_site.sh --db muscat.db  # explicit database path
#   scripts/deploy_static_site.sh --remote origin # explicit remote
#   scripts/deploy_static_site.sh --no-push       # build + guard only, print path
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)"
cd "$REPO_ROOT"

DB="muscat.db"
REMOTE="origin"
BRANCH="pages"
PUSH=1
while [ $# -gt 0 ]; do
  case "$1" in
    --db)     DB="$2"; shift 2 ;;
    --remote) REMOTE="$2"; shift 2 ;;
    --no-push) PUSH=0; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

BUILD_DIR="$(mktemp -d "${TMPDIR:-$HOME/temp}/muscat-site.XXXXXX")"
trap 'rm -rf "$BUILD_DIR"' EXIT
SITE="$BUILD_DIR/site"

echo "[deploy-site] building snapshot from ${DB} ..."
uv run muscat-db build-static-site --out "$SITE" --db "$DB"

# Guard: a build on a host without prose/timer outputs produces only empty
# parametric shells, and the home navbar then links to example pages that were
# never written — a site whose Photometry/Transit-fit links 404. Refuse to
# publish that broken snapshot rather than ship it.
missing=""
for parent in photometry transit-fit; do
  if [ -z "$(find "$SITE/$parent" -mindepth 1 -type d 2>/dev/null | head -n1)" ]; then
    missing="${missing} ${parent}"
  fi
done
if [ -n "$missing" ]; then
  echo "[deploy-site] ERROR: no example detail pages were produced for:${missing}" >&2
  echo "[deploy-site] The host is missing the prose/timer figure trees; the navbar would 404." >&2
  echo "[deploy-site] Run on the production host, or check MUSCAT_PROSE_DIR / MUSCAT_TIMER_DIR." >&2
  exit 1
fi

if [ "$PUSH" -eq 0 ]; then
  trap - EXIT   # keep the build for inspection
  echo "[deploy-site] built and validated at: $SITE (--no-push; not publishing)"
  exit 0
fi

REMOTE_URL="$(git remote get-url "$REMOTE")"
echo "[deploy-site] publishing to ${REMOTE} (${REMOTE_URL}) branch '${BRANCH}' (force, orphan) ..."

# Orphan-branch tree: the snapshot under site/ (the workflow uploads only that,
# never .git/.github), plus a copy of the Pages workflow so that pushing this
# branch self-triggers the deploy — GitHub reads workflows from the pushed ref,
# and an orphan branch carrying only site/ would never fire pages.yml.
PUB="$BUILD_DIR/pub"
mkdir -p "$PUB/site" "$PUB/.github/workflows"
cp -a "$SITE/." "$PUB/site/"
cp "$REPO_ROOT/.github/workflows/pages.yml" "$PUB/.github/workflows/pages.yml"
(
  cd "$PUB"
  git init -q
  git checkout -q --orphan "$BRANCH"
  git add -A
  git -c user.name="muscat-db site bot" \
      -c user.email="muscat-db@users.noreply.github.com" \
      commit -qm "Publish static snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  git push -q -f "$REMOTE_URL" "${BRANCH}:${BRANCH}"
)
echo "[deploy-site] done — pushed '${BRANCH}'. GitHub Actions (pages.yml) will deploy it."
