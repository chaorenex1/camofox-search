"""SQLite rule store for camofox-search.

Layout (per the plugin spec)::

    <HERMES_HOME>/camofox-search/camofox-search.db      (WAL journal mode)

    search_rules   id, domain, url_template, extract_js, notes, created_at, updated_at
    content_rules  id, domain, extract_js, notes, created_at, updated_at

- A **search rule** maps a search-engine domain (``bing.com``) to a SERP URL
  template (``{query}``/``{limit}`` placeholders) plus page-context extraction
  JS (``@LIMIT@`` placeholder). It drives ``web_search`` engine selection and
  can add engines with no built-in support.
- A **content rule** maps a content-page domain to page-context extraction JS.
  It overrides the generic full-page text extraction in ``web_extract`` for
  URLs on that domain (or any subdomain).

The table schema above is the contract; ``notes`` and the RFC-3339 timestamps
are bookkeeping the manager tool surfaces back. The built-in engines from
:mod:`engines` are seeded into ``search_rules`` when the plugin starts
(``INSERT OR IGNORE`` — an edited rule is never overwritten) so the table
doubles as documentation of the default behavior.

Resolution is by domain, so each entry of ``CAMOFOX_SEARCH_ENGINE`` (a
whitespace/comma-separated list, default ``duckduckgo google bing``) may be an
engine id (``bing``), a domain (``bing.com``) or a subdomain (``cn.bing.com``)
— all three pick the same rule; longest-domain matches win.

HERMES_HOME resolution order: ``CAMOFOX_SEARCH_DATA_DIR`` (explicit override,
used by tests) → ``hermes_constants.get_hermes_home()`` (profile-aware inside
the Hermes runtime) → ``HERMES_HOME`` env → platform default. Every function
resolves live — nothing is cached across profiles.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .engines import ENGINES

logger = logging.getLogger(__name__)

__all__ = [
    "DATA_DIR_ENV", "DB_FILENAME", "Rule", "RuleValidationError",
    "RuleNotFoundError", "RuleExistsError",
    "data_dir", "db_path", "ensure_db", "connect",
    "normalize_domain", "validate_url_template", "validate_extract_js",
    "list_search_rules", "get_search_rule", "resolve_search_rule",
    "create_search_rule", "update_search_rule", "delete_search_rule",
    "list_content_rules", "get_content_rule", "resolve_content_rule",
    "create_content_rule", "update_content_rule", "delete_content_rule",
    "count_rules",
]

DATA_DIR_ENV = "CAMOFOX_SEARCH_DATA_DIR"
DB_FILENAME = "camofox-search.db"
BUSY_TIMEOUT_MS = 5000
MAX_JS_CHARS = 20000
MAX_TEMPLATE_CHARS = 2000
MAX_NOTES_CHARS = 1000

_SEARCH_COLUMNS = "id, domain, url_template, extract_js, notes, created_at, updated_at"
_CONTENT_COLUMNS = "id, domain, extract_js, notes, created_at, updated_at"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS search_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    url_template TEXT NOT NULL,
    extract_js TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    extract_js TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class RuleStoreError(RuntimeError):
    """The rule database could not be opened or written."""


class RuleValidationError(ValueError):
    """A rule field failed validation (bad domain, template or JS)."""


class RuleNotFoundError(LookupError):
    """No rule exists for the requested domain."""


class RuleExistsError(ValueError):
    """A rule for this domain already exists (use update)."""


@dataclass(frozen=True)
class Rule:
    """One search or content rule as stored in the database."""

    id: int
    kind: str  # "search" | "content"
    domain: str
    extract_js: str
    url_template: Optional[str]
    notes: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "domain": self.domain,
        }
        if self.kind == "search":
            data["url_template"] = self.url_template
        data["extract_js"] = self.extract_js
        data["notes"] = self.notes
        data["created_at"] = self.created_at
        data["updated_at"] = self.updated_at
        return data


# ---- location -----------------------------------------------------------


def _default_hermes_home() -> Path:
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / ("hermes" + suffix)
    return Path.home() / (".hermes" + suffix)


def hermes_home() -> Path:
    """The active Hermes home: context override → HERMES_HOME → platform default."""
    try:
        from hermes_constants import get_hermes_home
    except Exception:  # pragma: no cover - standalone runs / repo tests
        get_hermes_home = None  # type: ignore[assignment]
    if get_hermes_home is not None:
        try:
            return Path(get_hermes_home())
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("camofox-search: get_hermes_home() failed: %s", exc)
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw) if raw else _default_hermes_home()


def data_dir() -> Path:
    """Plugin data directory: ``<HERMES_HOME>/camofox-search`` (env-overridable)."""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return hermes_home() / "camofox-search"


def db_path() -> Path:
    """Full path of the rule database file."""
    return data_dir() / DB_FILENAME


# ---- connection / schema -------------------------------------------------

_ensure_lock = threading.Lock()
_ENSURED_DBS: set[str] = set()


def _connect_path(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seed_builtin_engines(conn: sqlite3.Connection) -> None:
    """Insert the built-in engines as rules if their domain is absent.

    ``INSERT OR IGNORE`` on the unique domain: an existing (possibly edited)
    rule is left untouched, so the seeds only ever fill gaps.
    """
    now = _now()
    for name, engine in sorted(ENGINES.items()):
        conn.execute(
            "INSERT OR IGNORE INTO search_rules "
            "(domain, url_template, extract_js, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (engine.domain, engine.url_template, engine.js_template,
             f"Seed: built-in {name} engine (edit or delete freely)", now, now),
        )


def ensure_db() -> Path:
    """Create the data directory, database and seeded tables (once per process)."""
    path = db_path()
    key = str(path)
    if key in _ENSURED_DBS:
        return path
    with _ensure_lock:
        if key in _ENSURED_DBS:
            return path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = _connect_path(path)
            try:
                with conn:
                    conn.executescript(_SCHEMA_SQL)
                    _seed_builtin_engines(conn)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            raise RuleStoreError(
                f"cannot open the camofox-search rule database at {path}: {exc}") from exc
        _ENSURED_DBS.add(key)
        logger.info("camofox-search: rule database ready at %s", path)
    return path


def connect() -> sqlite3.Connection:
    """Open the rule database (schema ensured); caller closes the connection."""
    return _connect_path(ensure_db())


# ---- validation ----------------------------------------------------------


_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^(?:{_DOMAIN_LABEL}\.)+{_DOMAIN_LABEL}$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]{0,40})\}")


def normalize_domain(value: str) -> str:
    """Normalize a domain (or a full URL) to its canonical stored form.

    ``https://www.Example.com/page`` → ``example.com``; a leading ``www.`` is
    dropped because rules match the domain and every subdomain.
    """
    raw = str(value or "").strip()
    if not raw:
        raise RuleValidationError("domain is required")
    if "://" in raw:
        raw = urlsplit(raw).hostname or ""
    else:
        raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    raw = raw.strip().strip(".").lower()
    if raw.startswith("www."):
        raw = raw[4:]
    if len(raw) > 253:
        raise RuleValidationError("domain is longer than 253 characters")
    if raw == "localhost" or _DOMAIN_RE.fullmatch(raw):
        return raw
    raise RuleValidationError(
        f"invalid domain {value!r}: expected a hostname like 'example.com'")


def validate_url_template(template: str) -> str:
    """Validate a SERP URL template; returns the trimmed template."""
    text = str(template or "").strip()
    if not text:
        raise RuleValidationError("url_template is required for search rules")
    if len(text) > MAX_TEMPLATE_CHARS:
        raise RuleValidationError(
            f"url_template is longer than {MAX_TEMPLATE_CHARS} characters")
    if not text.startswith(("http://", "https://")):
        raise RuleValidationError("url_template must start with http:// or https://")
    unknown = sorted(set(_PLACEHOLDER_RE.findall(text)) - {"query", "limit"})
    if unknown:
        raise RuleValidationError(
            "url_template contains unknown placeholder(s): "
            + ", ".join("{" + name + "}" for name in unknown)
            + "; supported placeholders are {query} and {limit}")
    if "{query}" not in text:
        raise RuleValidationError(
            "url_template must contain the {query} placeholder "
            "(the URL-encoded search query)")
    return text


def validate_extract_js(js: str) -> str:
    """Validate an extraction JS expression; returns the trimmed expression."""
    text = str(js if js is not None else "").strip()
    if not text:
        raise RuleValidationError("extract_js is required")
    if len(text) > MAX_JS_CHARS:
        raise RuleValidationError(
            f"extract_js is longer than {MAX_JS_CHARS} characters")
    if "\x00" in text:
        raise RuleValidationError("extract_js must not contain NUL bytes")
    return text


def _clean_notes(notes: Optional[str]) -> str:
    return str(notes if notes is not None else "").strip()[:MAX_NOTES_CHARS]


def _host_of(value: str) -> str:
    raw = str(value or "").strip()
    if "://" in raw:
        return (urlsplit(raw).hostname or "").lower()
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return raw.strip().strip(".").lower()


# ---- search rules --------------------------------------------------------


def _search_rule(row: sqlite3.Row) -> Rule:
    return Rule(id=row["id"], kind="search", domain=row["domain"],
                url_template=row["url_template"], extract_js=row["extract_js"],
                notes=row["notes"], created_at=row["created_at"],
                updated_at=row["updated_at"])


def _content_rule(row: sqlite3.Row) -> Rule:
    return Rule(id=row["id"], kind="content", domain=row["domain"],
                url_template=None, extract_js=row["extract_js"],
                notes=row["notes"], created_at=row["created_at"],
                updated_at=row["updated_at"])


def list_search_rules() -> list[Rule]:
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_SEARCH_COLUMNS} FROM search_rules ORDER BY domain").fetchall()
    finally:
        conn.close()
    return [_search_rule(row) for row in rows]


def get_search_rule(domain: str) -> Optional[Rule]:
    dom = normalize_domain(domain)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_SEARCH_COLUMNS} FROM search_rules WHERE domain = ?",
            (dom,)).fetchone()
    finally:
        conn.close()
    return _search_rule(row) if row is not None else None


def _match_rank(domain: str, selector: str) -> Optional[tuple[int, int]]:
    """Rank a rule domain against a selector; lower is better, None = no match."""
    if domain == selector:
        return (0, -len(domain))
    if selector.endswith("." + domain):
        return (1, -len(domain))
    if domain.endswith("." + selector):
        return (2, len(domain))
    if selector and selector in domain:
        return (3, len(domain))
    return None


def resolve_search_rule(selector: str) -> Optional[Rule]:
    """Pick the search rule for an engine selector (id, domain or subdomain)."""
    raw = str(selector or "").strip().lower()
    if not raw:
        return None
    try:
        selector_text = normalize_domain(raw)
    except RuleValidationError:
        selector_text = raw.strip().strip(".")  # bare id like "bing"
    best: Optional[Rule] = None
    best_rank: Optional[tuple[int, int]] = None
    for rule in list_search_rules():
        rank = _match_rank(rule.domain, selector_text)
        if rank is not None and (best_rank is None or rank < best_rank):
            best, best_rank = rule, rank
    return best


def create_search_rule(domain: str, url_template: str, extract_js: str,
                       notes: str = "") -> Rule:
    dom = normalize_domain(domain)
    tpl = validate_url_template(url_template)
    js = validate_extract_js(extract_js)
    now = _now()
    conn = connect()
    try:
        try:
            with conn:
                conn.execute(
                    "INSERT INTO search_rules "
                    "(domain, url_template, extract_js, notes, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (dom, tpl, js, _clean_notes(notes), now, now))
        except sqlite3.IntegrityError as exc:
            raise RuleExistsError(
                f"a search rule for {dom!r} already exists; use action='update'") from exc
    finally:
        conn.close()
    rule = get_search_rule(dom)
    assert rule is not None  # just inserted
    return rule


def update_search_rule(domain: str, *, url_template: Optional[str] = None,
                       extract_js: Optional[str] = None,
                       notes: Optional[str] = None) -> Rule:
    dom = normalize_domain(domain)
    existing = get_search_rule(dom)
    if existing is None:
        raise RuleNotFoundError(
            f"no search rule for {dom!r}; use action='create' first")
    if url_template is None and extract_js is None and notes is None:
        raise RuleValidationError(
            "nothing to update: pass url_template, extract_js and/or notes")
    tpl = validate_url_template(url_template) if url_template is not None else existing.url_template
    js = validate_extract_js(extract_js) if extract_js is not None else existing.extract_js
    note_text = existing.notes if notes is None else _clean_notes(notes)
    conn = connect()
    try:
        with conn:
            conn.execute(
                "UPDATE search_rules SET url_template = ?, extract_js = ?, "
                "notes = ?, updated_at = ? WHERE domain = ?",
                (tpl, js, note_text, _now(), dom))
    finally:
        conn.close()
    rule = get_search_rule(dom)
    assert rule is not None
    return rule


def delete_search_rule(domain: str) -> Rule:
    dom = normalize_domain(domain)
    existing = get_search_rule(dom)
    if existing is None:
        raise RuleNotFoundError(f"no search rule for {dom!r} to delete")
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM search_rules WHERE domain = ?", (dom,))
    finally:
        conn.close()
    return existing


# ---- content rules -------------------------------------------------------


def list_content_rules() -> list[Rule]:
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_CONTENT_COLUMNS} FROM content_rules ORDER BY domain").fetchall()
    finally:
        conn.close()
    return [_content_rule(row) for row in rows]


def get_content_rule(domain: str) -> Optional[Rule]:
    dom = normalize_domain(domain)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_CONTENT_COLUMNS} FROM content_rules WHERE domain = ?",
            (dom,)).fetchone()
    finally:
        conn.close()
    return _content_rule(row) if row is not None else None


def resolve_content_rule(url_or_host: str) -> Optional[Rule]:
    """Pick the content rule whose domain is the host or one of its parents."""
    host = _host_of(url_or_host)
    if not host:
        return None
    best: Optional[Rule] = None
    best_rank: Optional[tuple[int, int]] = None
    for rule in list_content_rules():
        domain = rule.domain
        if host == domain:
            rank: tuple[int, int] = (0, -len(domain))
        elif host.endswith("." + domain):
            rank = (1, -len(domain))
        else:
            continue
        if best_rank is None or rank < best_rank:
            best, best_rank = rule, rank
    return best


def create_content_rule(domain: str, extract_js: str, notes: str = "") -> Rule:
    dom = normalize_domain(domain)
    js = validate_extract_js(extract_js)
    now = _now()
    conn = connect()
    try:
        try:
            with conn:
                conn.execute(
                    "INSERT INTO content_rules "
                    "(domain, extract_js, notes, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (dom, js, _clean_notes(notes), now, now))
        except sqlite3.IntegrityError as exc:
            raise RuleExistsError(
                f"a content rule for {dom!r} already exists; use action='update'") from exc
    finally:
        conn.close()
    rule = get_content_rule(dom)
    assert rule is not None
    return rule


def update_content_rule(domain: str, *, extract_js: Optional[str] = None,
                        notes: Optional[str] = None) -> Rule:
    dom = normalize_domain(domain)
    existing = get_content_rule(dom)
    if existing is None:
        raise RuleNotFoundError(
            f"no content rule for {dom!r}; use action='create' first")
    if extract_js is None and notes is None:
        raise RuleValidationError("nothing to update: pass extract_js and/or notes")
    js = validate_extract_js(extract_js) if extract_js is not None else existing.extract_js
    note_text = existing.notes if notes is None else _clean_notes(notes)
    conn = connect()
    try:
        with conn:
            conn.execute(
                "UPDATE content_rules SET extract_js = ?, notes = ?, updated_at = ? "
                "WHERE domain = ?",
                (js, note_text, _now(), dom))
    finally:
        conn.close()
    rule = get_content_rule(dom)
    assert rule is not None
    return rule


def delete_content_rule(domain: str) -> Rule:
    dom = normalize_domain(domain)
    existing = get_content_rule(dom)
    if existing is None:
        raise RuleNotFoundError(f"no content rule for {dom!r} to delete")
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM content_rules WHERE domain = ?", (dom,))
    finally:
        conn.close()
    return existing


# ---- summary -------------------------------------------------------------


def count_rules() -> dict[str, int]:
    """Rule counts per table (cheap; used by diagnostics/smoke)."""
    conn = connect()
    try:
        search = conn.execute("SELECT COUNT(*) FROM search_rules").fetchone()[0]
        content = conn.execute("SELECT COUNT(*) FROM content_rules").fetchone()[0]
    finally:
        conn.close()
    return {"search_rules": int(search), "content_rules": int(content)}
