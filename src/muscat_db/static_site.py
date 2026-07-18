"""Build a static, navigable snapshot of the muscat-db UI for GitHub Pages.

This drives the real FastAPI app (``muscat_db.web:app``) through Starlette's
``TestClient`` — the same object the test-suite uses — so every page is produced
by the *real* route handlers reading a real ``muscat.db``. The captured HTML is
then rewritten into a self-contained, relatively-linked static tree that can be
served by any static host (GitHub Pages, ``python -m http.server``, …) with no
backend, database, or conda stack.

Design notes
------------
* **Representative subset, not a full mirror.** All navigation pages plus a few
  example detail / drill-down pages (chosen from what actually has data and
  figures on disk). This keeps the published site small while still documenting
  the whole UI.
* **Figures are copied by re-fetching the real ``/file/…`` route.** Rather than
  duplicating the on-disk path resolution, each ``/api/(photometry|transit-fit)/
  file/…`` URL referenced by a captured page is fetched through the same
  ``TestClient`` and written under ``assets/``; the reference is rewritten to the
  local copy. Missing files are skipped, never fatal.
* **Links are depth-relative** (``../`` per URL segment), so the result works
  under a project site (``user.github.io/repo/``), a user site, or a custom
  domain with no hard-coded base path. ``--base-path`` can force root-absolute
  links instead.
* **Live-API pages** (ephemeris, fov, exposure, lco) render as static shells; a
  banner is injected explaining that live data and actions are disabled.
* **Privacy.** ``scrub_notes`` (default on) blanks user-authored target notes and
  job usernames at the data layer before capture, so private text is never
  written to the published site.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

# ── constants ────────────────────────────────────────────────────────────────

# Pages captured with no query parameters. Order controls nothing here; the
# navbar defines the visible order. The three parametric parents
# (``/photometry``, ``/transit-fit``, ``/target``) are captured as empty-shell
# fallbacks so the navbar never dead-links even when no example page exists; when
# an example *is* found its populated detail page is what the navbar points at
# (see ``_PARAM_PARENTS`` handling in ``_enumerate``).
_NAV_PAGES: tuple[str, ...] = (
    "/",
    "/logs",
    "/guide",
    "/jobs",
    "/settings",
    "/toi",
    "/nexsci",
    "/ephemeris",
    "/exposure",
    "/fov",
    "/lco/schedule",
    "/lco/archive",
    "/photometry",
    "/transit-fit",
    "/target",
)

# Parent routes whose navbar link should resolve to a populated example detail
# page when one exists, falling back to the captured empty shell otherwise.
_PARAM_PARENTS: frozenset[str] = frozenset({"/photometry", "/transit-fit", "/target"})

# 6-digit observation date, e.g. 231201.
_DATE_RE = re.compile(r"^\d{6}$")

# Figure/download routes whose targets we copy into the static tree.
_FILE_ROUTES: tuple[tuple[str, str], ...] = (
    ("/api/photometry/file/", "assets/photometry/"),
    ("/api/transit-fit/file/", "assets/transit-fit/"),
)

# The live pages also link to logs, tables, archives, and model configuration
# through the same file routes.  Those are useful downloads in the application,
# but the public snapshot is visual documentation: publish only assets that a
# browser displays as figures and disable the other download links.
_FIGURE_SUFFIXES: frozenset[str] = frozenset({".gif", ".png"})

# Link schemes/prefixes we never rewrite.
_EXTERNAL_RE = re.compile(r"^(?:[a-z]+:|//|#|data:|mailto:|tel:|javascript:)", re.I)

_BANNER_MARKER = "<!--muscat-static-snapshot-banner-->"


@dataclass
class BuildStats:
    """Summary of a build, returned to the CLI for reporting."""

    pages: int = 0
    figures: int = 0
    figures_missing: int = 0
    skipped: list[str] = field(default_factory=list)


# ── small helpers ────────────────────────────────────────────────────────────


def _slug(name: str) -> str:
    """URL/filesystem-safe directory component for a target name.

    Mirrors the route's space-stripping so the slug round-trips through the
    ``?target=`` / ``?name=`` query the page expects.
    """
    stripped = (name or "").replace(" ", "")
    return re.sub(r"[^A-Za-z0-9._+-]", "_", stripped) or "_"


def _banner_html(dismissible: bool = True) -> str:
    """A slim, self-contained banner injected at the top of every page.

    Styles are inlined so it renders identically without depending on
    ``styles.css`` load order.
    """
    close = (
        '<button type="button" aria-label="Dismiss" '
        'onclick="this.closest(\'#snapshot-banner\').remove()" '
        'style="margin-left:auto;background:none;border:0;color:inherit;'
        'font-size:1.1rem;line-height:1;cursor:pointer;padding:0 .3rem;">&times;</button>'
        if dismissible
        else ""
    )
    return (
        f"{_BANNER_MARKER}\n"
        '<div id="snapshot-banner" role="note" style="display:flex;align-items:center;'
        "gap:.6rem;padding:.5rem .9rem;background:#3a2d00;color:#ffe9a8;"
        "font:500 .85rem/1.3 system-ui,sans-serif;border-bottom:1px solid #5a4700;\">"
        "<span>&#128196; Static documentation snapshot of <strong>muscat-db</strong> "
        "&mdash; live data &amp; actions are disabled.</span>"
        f"{close}</div>"
    )


def _inject_banner(html: str) -> str:
    """Insert the banner immediately after the opening ``<body>`` tag."""
    if _BANNER_MARKER in html:
        return html
    m = re.search(r"<body[^>]*>", html, re.I)
    if not m:
        return html
    idx = m.end()
    return html[:idx] + "\n" + _banner_html() + html[idx:]


def _url_to_sitedir(path: str, query: str = "") -> str:
    """Map a captured URL to its output directory (relative, no leading slash).

    ``/`` → ``""`` (site root), ``/logs`` → ``logs``,
    ``/muscat/231201/ccd0`` → ``muscat/231201/ccd0``,
    ``/target?name=X`` → ``target/<slug>``,
    ``/photometry?inst=&date=&target=`` → ``photometry/<inst>/<date>/<slug>``.
    """
    p = path.strip("/")
    if not p:
        return ""
    qs = parse_qs(query)
    if p == "target":
        name = (qs.get("name") or [""])[0]
        return f"target/{_slug(name)}" if name else "target"
    if p in ("photometry", "transit-fit"):
        inst = (qs.get("inst") or [""])[0]
        date = (qs.get("date") or [""])[0]
        target = (qs.get("target") or [""])[0]
        if inst and date and target:
            return f"{p}/{inst}/{date}/{_slug(target)}"
    return p


# ── environment + privacy scrubbing ──────────────────────────────────────────


def _prepare_env(db_path: str) -> None:
    """Point the app at ``db_path`` and disable proxy auth for the build.

    All captured routes are GET, so CSRF never triggers; we only need to ensure
    authentication is not *required* (which would 401 every page). The LCO
    observation monitor is disabled so the build never spins up a background
    network-polling thread.
    """
    os.environ["MUSCAT_DB_PATH"] = str(Path(db_path).resolve())
    os.environ["MUSCAT_REQUIRE_AUTH"] = "0"
    os.environ["MUSCAT_LCO_MONITOR_ENABLED"] = "0"
    os.environ.pop("MUSCAT_PROXY_SECRET", None)


# Data-layer helpers we override to scrub private text. Captured pristine once
# so repeated builds in one process never wrap an already-wrapped function or
# leak a previous build's scrub state.
_SCRUB_TARGETS: tuple[str, ...] = (
    "_get_targets",
    "_get_datasets_for_normalized_target",
    "_jobs_with_lco_archive_rows",
)

# Keep the published Jobs snapshot compact while still illustrating each job
# category represented in the source data.
_STATIC_JOB_EXAMPLES_PER_TYPE = 2


def _pristine(web) -> dict:
    """The unpatched originals, captured the first time we touch ``web``."""
    if not hasattr(web, "_static_site_pristine"):
        web._static_site_pristine = {name: getattr(web, name) for name in _SCRUB_TARGETS}
    return web._static_site_pristine


def _restore(web) -> None:
    """Reset the overridden helpers to their pristine originals."""
    for name, func in _pristine(web).items():
        setattr(web, name, func)
    orphan_fits = getattr(web, "_static_site_pristine_orphan_fits", None)
    if orphan_fits is not None:
        web.fit._discover_orphan_fits = orphan_fits


def _install_scrub(web) -> None:
    """Blank user-authored notes and job usernames at the data layer.

    Wrapping the pristine module-level helpers keeps ``web.py`` free of any
    snapshot awareness while guaranteeing private text never reaches the rendered
    HTML (including the ``data-note`` / ``data-search`` attributes built from it).
    """
    orig = _pristine(web)
    if not hasattr(web, "_static_site_pristine_orphan_fits"):
        web._static_site_pristine_orphan_fits = web.fit._discover_orphan_fits

    def targets_scrubbed(db):
        return [{**r, "note": ""} for r in orig["_get_targets"](db)]

    def datasets_scrubbed(db, normalized_name):
        datasets, last = orig["_get_datasets_for_normalized_target"](db, normalized_name)
        return [{**d, "note": ""} for d in datasets], last

    def jobs_scrubbed():
        selected: list[dict] = []
        counts: dict[str, int] = {}
        for job in orig["_jobs_with_lco_archive_rows"]():
            job_type = job.get("type", "photometry")
            if counts.get(job_type, 0) >= _STATIC_JOB_EXAMPLES_PER_TYPE:
                continue
            counts[job_type] = counts.get(job_type, 0) + 1
            selected.append({**job, "user_name": ""})
        return selected

    web._get_targets = targets_scrubbed
    web._get_datasets_for_normalized_target = datasets_scrubbed
    web._jobs_with_lco_archive_rows = jobs_scrubbed
    # Orphan discovery scans the live fit-output tree and can add a large,
    # non-reproducible history after the compact DB rows above are selected.
    web.fit._discover_orphan_fits = lambda _existing: []


def _scrub_host_paths(html: str) -> str:
    """Replace user-specific home prefixes embedded in rendered commands."""
    home = Path.home()
    prefixes = {
        str(home),
        f"/ut2/{home.name}",
        f"/raid_ut2/home/{home.name}",
    }
    for prefix in sorted(prefixes, key=len, reverse=True):
        html = html.replace(prefix, "~")
    return html


# Process-level caches in web.py keyed on DB/template mtime (not path or scrub
# state). Clearing them before a build makes captures deterministic and keeps a
# second in-process build from serving the first build's cached HTML.
_WEB_CACHE_ATTRS: tuple[str, ...] = (
    "_CATALOG_CACHE",
    "_index_cache",
    "_toi_cache",
    "_toi_db_cache",
    "_nexsci_cache",
    "_harps_cache",
    "_boyle_cache",
)


def _clear_web_caches(web) -> None:
    for attr in _WEB_CACHE_ATTRS:
        cache = getattr(web, attr, None)
        if cache is not None and hasattr(cache, "clear"):
            cache.clear()


# ── URL enumeration (representative subset) ───────────────────────────────────


def _drilldown_urls(database, instruments) -> list[str]:
    """One ``/{inst}`` + newest ``/{inst}/{date}`` + first ``ccd`` per instrument
    that has data."""
    db = os.environ["MUSCAT_DB_PATH"]
    urls: list[str] = []
    for inst in instruments:
        dates = database.get_dates(db, inst)
        if not dates:
            continue
        urls.append(f"/{inst}")
        date = dates[0]["obsdate"]
        urls.append(f"/{inst}/{date}")
        summaries = database.get_summaries(db, inst, date)
        ccds = sorted({s["ccd"] for s in summaries})
        if ccds:
            urls.append(f"/{inst}/{date}/ccd{ccds[0]}")
    return urls


def _photometry_examples(phot, instruments, limit: int) -> list[tuple[str, str, str]]:
    """Find up to ``limit`` (inst, date, target) tuples with photometry products
    already on disk, reusing prose's own discovery helpers."""
    found: list[tuple[str, str, str]] = []
    for inst in instruments:
        for date in phot.output_dates(inst):
            for target in phot.discovered_targets(inst, date):
                try:
                    if phot.list_outputs(inst, date, target).get("has_any"):
                        found.append((inst, date, target))
                except Exception:
                    continue
                if len(found) >= limit:
                    return found
    return found


def _transit_fit_examples(instruments, limit: int) -> list[tuple[str, str, str]]:
    """Find up to ``limit`` (inst, date, target) tuples with timer outputs by
    walking the timer root (``$MUSCAT_TIMER_DIR``)."""
    base = Path(
        os.environ.get("MUSCAT_TIMER_DIR", str(Path.home() / "ql" / "timer"))
    ).expanduser()
    if not base.is_dir():
        return []
    found: list[tuple[str, str, str]] = []

    def _has_png(d: Path) -> bool:
        out = d / "out"
        return out.is_dir() and any(out.glob("*.png"))

    for inst in instruments:
        inst_dir = base / inst
        if not inst_dir.is_dir():
            continue
        for date_dir in sorted(inst_dir.iterdir(), reverse=True):
            if not date_dir.is_dir() or not _DATE_RE.match(date_dir.name):
                continue
            for target_dir in sorted(date_dir.iterdir()):
                if not target_dir.is_dir():
                    continue
                # Legacy layout (out/ under the target) or per-run subdirs.
                if _has_png(target_dir) or any(
                    _has_png(rd) for rd in target_dir.iterdir() if rd.is_dir()
                ):
                    found.append((inst, date_dir.name, target_dir.name))
                if len(found) >= limit:
                    return found
    return found


@dataclass
class _Capture:
    url: str
    sitedir: str


def _enumerate(database, phot, instruments, n_examples: int) -> tuple[list[_Capture], dict[str, str]]:
    """Build the capture list and the ``path → sitedir`` route map used to
    rewrite navbar links so they land on populated example pages."""
    captures: list[_Capture] = []
    route_map: dict[str, str] = {}
    seen: set[str] = set()

    def add(url: str) -> str:
        split = urlsplit(url)
        sitedir = _url_to_sitedir(split.path, split.query)
        if url not in seen:
            seen.add(url)
            captures.append(_Capture(url=url, sitedir=sitedir))
        return sitedir

    # No-parameter nav pages. Parametric parents are captured as fallback
    # shells but are left out of the route map here so a found example page can
    # claim the navbar link instead.
    for url in _NAV_PAGES:
        sitedir = add(url)
        key = urlsplit(url).path.rstrip("/") or "/"
        if key not in _PARAM_PARENTS:
            route_map.setdefault(key, sitedir)

    # Instrument drill-downs (also seed the obslog nav path map).
    for url in _drilldown_urls(database, instruments):
        sitedir = add(url)
        route_map.setdefault(urlsplit(url).path, sitedir)

    db = os.environ["MUSCAT_DB_PATH"]

    # Example detail pages: photometry, transit-fit, then target pages that tie
    # them together. The navbar link for each parent route is rewritten to the
    # first example so clicking it shows a populated page rather than an empty
    # shell.
    example_targets: list[str] = []

    for inst, date, target in _photometry_examples(phot, instruments, n_examples):
        q = urlencode({"inst": inst, "date": date, "target": target.replace(" ", "")})
        sitedir = add(f"/photometry?{q}")
        route_map.setdefault("/photometry", sitedir)
        example_targets.append(target)

    for inst, date, target in _transit_fit_examples(instruments, n_examples):
        q = urlencode({"inst": inst, "date": date, "target": target.replace(" ", "")})
        sitedir = add(f"/transit-fit?{q}")
        route_map.setdefault("/transit-fit", sitedir)
        example_targets.append(target)

    # Target pages: prefer the example targets, then top rows of the DB.
    target_names = list(dict.fromkeys(example_targets))
    if len(target_names) < n_examples:
        for row in database.get_targets(db)[: n_examples * 2]:
            target_names.append(row["object"])
    for name in list(dict.fromkeys(target_names))[: max(n_examples, 1) + len(example_targets)]:
        sitedir = add(f"/target?{urlencode({'name': name})}")
        route_map.setdefault("/target", sitedir)

    return captures, route_map


# ── HTML rewriting ───────────────────────────────────────────────────────────


def _prefix_for(sitedir: str, base_path: str) -> str:
    """Relative (or root-absolute) link prefix for a page in ``sitedir``."""
    if base_path:
        return base_path.rstrip("/") + "/"
    depth = 0 if sitedir == "" else sitedir.count("/") + 1
    return "../" * depth if depth else ""


def _rewrite_link(
    value: str,
    prefix: str,
    route_map: dict[str, str],
    figures: dict[str, str],
) -> str:
    """Rewrite one absolute-internal URL to its static-tree location.

    Returns the value unchanged when it is external, a fragment, or already
    relative. Records figure/download URLs in ``figures`` (url → asset path) for
    later fetching.
    """
    if not value or _EXTERNAL_RE.match(value) or not value.startswith("/"):
        return value

    split = urlsplit(value)
    path = split.path

    # Figure / download files: copy under assets/ and point at the local copy.
    for route, asset_root in _FILE_ROUTES:
        if path.startswith(route):
            rest = path[len(route):]
            rest_path = Path(rest)
            if (
                rest_path.is_absolute()
                or ".." in rest_path.parts
                or rest_path.suffix.lower() not in _FIGURE_SUFFIXES
            ):
                return "#"
            asset_rel = asset_root + rest
            # Record the original (query-bearing) URL for fetching; the copier
            # strips the ?v= cache-buster. The rewritten reference is the clean
            # local path so it is identical across pages.
            figures[value] = asset_rel
            return prefix + asset_rel

    # Bundled static assets: drop the ?v= cache-buster.
    if path.startswith("/static/"):
        return prefix + path.lstrip("/")

    # Query-bearing detail links must retain their identity.  The queryless
    # navbar parent intentionally points at the first populated example, but a
    # link for a different target/dataset should resolve to its own static-tree
    # location (which may be outside the representative snapshot and 404) rather
    # than silently showing the wrong example.
    key = path.rstrip("/") or "/"
    if split.query and key in _PARAM_PARENTS:
        target = _url_to_sitedir(path, split.query)
        if target != path.strip("/"):
            return prefix + target.rstrip("/") + "/"

    # Known page routes → their generated example/detail directory.
    if key in route_map:
        target = route_map[key]
        return prefix + (target + "/" if target else "")

    # Any other internal absolute link: keep it inside the site tree (it may be
    # an out-of-snapshot detail page and simply 404 locally, which is preferable
    # to escaping to the host root). Page-like paths get a trailing slash.
    trimmed = path.lstrip("/")
    if "." not in path.rsplit("/", 1)[-1]:
        trimmed = trimmed.rstrip("/") + "/"
    return prefix + trimmed


def _rewrite_html(
    html: str,
    sitedir: str,
    route_map: dict[str, str],
    base_path: str,
    figures: dict[str, str],
) -> str:
    """Rewrite all internal ``href``/``src``/``action`` links in a page and
    inject the snapshot banner."""
    prefix = _prefix_for(sitedir, base_path)

    def repl(m: re.Match) -> str:
        attr, quote, value = m.group("attr"), m.group("q"), m.group("v")
        new = _rewrite_link(value, prefix, route_map, figures)
        return f"{attr}={quote}{new}{quote}"

    pattern = re.compile(
        r'(?P<attr>href|src|action)=(?P<q>["\'])(?P<v>[^"\']*)(?P=q)', re.I
    )
    html = pattern.sub(repl, html)
    return _inject_banner(html)


# ── orchestration ────────────────────────────────────────────────────────────


def _write_page(out_dir: Path, sitedir: str, html: str) -> None:
    page_dir = out_dir / sitedir if sitedir else out_dir
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "index.html").write_text(html, encoding="utf-8")


def _copy_static(web, out_dir: Path) -> None:
    src = Path(web.STATIC_DIR)
    if src.is_dir():
        shutil.copytree(src, out_dir / "static", dirs_exist_ok=True)


def _validate_output_dir(out: Path, db_path: str) -> None:
    """Reject output paths whose cleanup could remove source or database data."""
    resolved = out.resolve()
    resolved_db = Path(db_path).resolve()
    if out.is_symlink():
        raise ValueError(f"refusing to replace symlinked output directory: {out}")
    if resolved in (resolved_db, *resolved_db.parents):
        raise ValueError(
            f"output directory {out} contains the database or one of its parents"
        )


def build_site(
    out_dir: str | Path,
    *,
    db_path: str | None = None,
    scrub_notes: bool = True,
    base_path: str = "",
    n_examples: int = 2,
    include_figures: bool = True,
    log: Callable[[str], None] = print,
) -> BuildStats:
    """Build the static site into ``out_dir`` and return build statistics."""
    from starlette.testclient import TestClient

    from muscat_db import database
    from muscat_db import photometry as phot
    from muscat_db.instruments import INSTRUMENTS

    resolved_db = db_path or os.environ.get("MUSCAT_DB_PATH", "muscat.db")
    out = Path(out_dir)
    _validate_output_dir(out, resolved_db)
    _prepare_env(resolved_db)

    # Import the app only after the environment is set so module-level config
    # (DB path, auth) is read correctly.
    from muscat_db import web

    _clear_web_caches(web)
    _restore(web)  # start from pristine helpers regardless of prior in-process builds
    if scrub_notes:
        _install_scrub(web)

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    instruments = list(INSTRUMENTS)
    captures, route_map = _enumerate(database, phot, instruments, n_examples)

    stats = BuildStats()
    figures: dict[str, str] = {}

    try:
        with TestClient(web.app, follow_redirects=True) as client:
            for cap in captures:
                try:
                    resp = client.get(cap.url)
                except Exception as exc:  # never let one page abort the build
                    stats.skipped.append(f"{cap.url} ({type(exc).__name__}: {exc})")
                    log(f"  skip {cap.url}: {exc}")
                    continue
                if resp.status_code != 200 or "text/html" not in resp.headers.get(
                    "content-type", ""
                ):
                    stats.skipped.append(f"{cap.url} (HTTP {resp.status_code})")
                    log(f"  skip {cap.url}: HTTP {resp.status_code}")
                    continue
                page_figs: dict[str, str] = {}
                html = _rewrite_html(
                    resp.text, cap.sitedir, route_map, base_path, page_figs
                )
                if scrub_notes:
                    html = _scrub_host_paths(html)
                _write_page(out, cap.sitedir, html)
                stats.pages += 1
                figures.update(page_figs)
                log(f"  page {cap.url} -> {cap.sitedir or '(root)'}/index.html")

            if include_figures:
                _copy_figures(client, out, figures, stats, log)
    finally:
        # Always leave the web module in its pristine state so the scrub
        # overrides never leak into a later in-process caller (e.g. the test
        # suite sharing the interpreter).
        _restore(web)

    _copy_static(web, out)
    (out / ".nojekyll").write_text("", encoding="utf-8")

    log(
        f"Done: {stats.pages} pages, {stats.figures} figures "
        f"({stats.figures_missing} missing), {len(stats.skipped)} skipped."
    )
    return stats


def _copy_figures(
    client,
    out: Path,
    figures: dict[str, str],
    stats: BuildStats,
    log: Callable[[str], None],
) -> None:
    """Fetch each referenced figure/download through the real ``/file/…`` route
    and write it to its ``assets/`` location. Missing files are counted, never
    fatal."""
    for url, asset_rel in figures.items():
        # The file route ignores the ?v= cache-buster; fetch the bare path.
        fetch_url = urlsplit(url)._replace(query="").geturl()
        dest = out / asset_rel
        if dest.exists():
            continue
        try:
            resp = client.get(fetch_url)
        except Exception as exc:
            stats.figures_missing += 1
            log(f"  figure miss {fetch_url}: {exc}")
            continue
        if resp.status_code != 200:
            stats.figures_missing += 1
            log(f"  figure miss {fetch_url}: HTTP {resp.status_code}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        stats.figures += 1
