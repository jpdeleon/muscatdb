#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$REPO/data"
TMPDIR="${MUSCAT_TMPDIR:-$HOME/temp}"
mkdir -p "$TMPDIR"
BASE_URL="https://exoplanetarchive.ipac.caltech.edu/TAP/sync"

download_csv() {
    local query="$1"
    local label="$2"
    local out="$3"
    local tmp="$(mktemp "$TMPDIR/${label}_XXXXXX.csv")"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Downloading $label -> $out ..."
    if curl -sS -o "$tmp" -w "HTTP %{http_code}, %{size_download} bytes\n" \
         --data-urlencode "query=$query" \
         --data-urlencode "format=csv" \
         "$BASE_URL" --max-time 300; then
        mv -f "$tmp" "$DATA/$out"
        echo "  -> $DATA/$out updated"
    else
        echo "  WARNING: $label download failed, keeping existing $out"
        rm -f "$tmp"
    fi
}

download_csv \
  "SELECT tid AS \"TIC ID\", toi AS \"TOI\", toidisplay AS \"Planet Name\", tfopwg_disp AS \"TFOPWG Disposition\", pl_orbper AS \"Period (days)\", pl_trandurh AS \"Duration (hours)\", pl_trandep AS \"Depth (ppm)\", pl_rade AS \"Planet Radius (R_Earth)\", pl_eqt AS \"Planet Equil Temp (K)\", pl_insol AS \"Planet Insolation (Earth Flux)\", st_tmag AS \"TESS Mag\", st_teff AS \"Stellar Eff Temp (K)\", st_rad AS \"Stellar Radius (R_Sun)\", st_dist AS \"Stellar Distance (pc)\", ra AS \"ra_deg\", dec AS \"dec_deg\", pl_orbpererr1 AS \"Period (days) err\", pl_trandurherr1 AS \"Duration (hours) err\", pl_trandeperr1 AS \"Depth (ppm) err\", pl_radeerr1 AS \"Planet Radius (R_Earth) err\", st_tmagerr1 AS \"TESS Mag err\", st_tefferr1 AS \"Stellar Eff Temp (K) err\", st_raderr1 AS \"Stellar Radius (R_Sun) err\", st_disterr1 AS \"Stellar Distance (pc) err\" FROM toi" \
  "toi" "TOIs.csv"

download_csv \
  "SELECT pl_name, hostname, tic_id, discoverymethod, disc_facility, st_spectype, disc_year, ra AS ra_x, dec AS dec_x, pl_orbper, pl_orbsmax, pl_rade, pl_radj, pl_bmasse, pl_bmassj, pl_bmassprov, pl_eqt, pl_insol, pl_ratror, pl_trandep, pl_trandur, pl_imppar, pl_orbincl, pl_orbeccen, pl_dens, st_teff, st_rad, st_mass, st_logg, st_met, st_dens, sy_dist, sy_vmag, sy_tmag, sy_gaiamag, sy_kmag, sy_snum, cb_flag, st_age, st_ageerr1, st_agelim, ttv_flag, pl_projobliq, st_nrvc, st_nspec, st_nphot FROM pscomppars" \
  "pscomppars" "nexsci_pscomppars.csv"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Catalog download complete"
