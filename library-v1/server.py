#!/usr/bin/env python3
"""Home Library V1: a dependency-free HTTP API backed by SQLite."""

from __future__ import annotations

import json
import html as html_lib
import base64
import binascii
import mimetypes
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("LIBRARY_DATA_DIR", APP_DIR / "data"))
DB_PATH = DATA_DIR / "library.db"
HOST = os.environ.get("LIBRARY_HOST", "0.0.0.0")
PORT = int(os.environ.get("LIBRARY_PORT", "8080"))
STATUSES = {"tbr", "in_progress", "finished", "dnf"}
OWNERSHIP_STATES = {"owned", "ku", "need_to_purchase"}
MAX_BODY_BYTES = 10_000_000
MAX_COVER_DATA_BYTES = 1_000_000
GOODREADS_BOOK_CACHE: dict[str, dict[str, Any]] = {}
GOODREADS_SERIES_CACHE: dict[str, list[dict[str, Any]]] = {}

# Small, source-verified corrections for incomplete Open Library work records.
# Keep these keyed by stable work ID so title collisions cannot apply them broadly.
WORK_METADATA_CORRECTIONS: dict[str, dict[str, str]] = {
    "/works/OL45106192W": {"series": "Into Darkness", "volume": "3"},  # Game On — Navessa Allen
}


BOOKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '',
    isbn TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    open_library_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'tbr'
        CHECK(status IN ('tbr', 'in_progress', 'finished', 'dnf')),
    progress INTEGER NOT NULL DEFAULT 0
        CHECK(progress BETWEEN 0 AND 100),
    current_page INTEGER NOT NULL DEFAULT 0
        CHECK(current_page BETWEEN 0 AND 100000),
    rating INTEGER NOT NULL DEFAULT 0
        CHECK(rating BETWEEN 0 AND 5),
    series TEXT NOT NULL DEFAULT '',
    volume TEXT NOT NULL DEFAULT '',
    total_pages INTEGER NOT NULL DEFAULT 0
        CHECK(total_pages BETWEEN 0 AND 100000),
    formats TEXT NOT NULL DEFAULT '["physical"]',
    ownership TEXT NOT NULL DEFAULT 'owned'
        CHECK(ownership IN ('owned', 'ku', 'need_to_purchase')),
    notes TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db_connection() as connection:
        connection.execute(BOOKS_TABLE_SQL)
        table_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'books'"
        ).fetchone()
        table_sql = str(table_row["sql"] or "") if table_row else ""
        if "'dnf'" not in table_sql:
            # SQLite cannot alter a CHECK constraint in place. Rebuild the table
            # transactionally, copying only columns that existed in the old app.
            connection.execute("ALTER TABLE books RENAME TO books_before_v11")
            connection.execute(BOOKS_TABLE_SQL)
            old_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(books_before_v11)").fetchall()
            }
            new_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(books)").fetchall()
            }
            shared_columns = [
                name for name in (
                    "id", "title", "authors", "isbn", "cover_url", "open_library_key", "status",
                    "progress", "current_page", "rating", "series", "volume", "total_pages", "formats",
                    "ownership", "notes", "started_at", "finished_at", "created_at", "updated_at",
                )
                if name in old_columns and name in new_columns
            ]
            columns_sql = ", ".join(shared_columns)
            connection.execute(
                f"INSERT INTO books ({columns_sql}) SELECT {columns_sql} FROM books_before_v11"
            )
            connection.execute("DROP TABLE books_before_v11")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(books)").fetchall()}
        if "total_pages" not in columns:
            connection.execute(
                "ALTER TABLE books ADD COLUMN total_pages INTEGER NOT NULL DEFAULT 0 "
                "CHECK(total_pages BETWEEN 0 AND 100000)"
            )
        if "current_page" not in columns:
            connection.execute(
                "ALTER TABLE books ADD COLUMN current_page INTEGER NOT NULL DEFAULT 0 "
                "CHECK(current_page BETWEEN 0 AND 100000)"
            )
        if "formats" not in columns:
            connection.execute(
                "ALTER TABLE books ADD COLUMN formats TEXT NOT NULL DEFAULT '[\"physical\"]'"
            )
        if "ownership" not in columns:
            connection.execute(
                "ALTER TABLE books ADD COLUMN ownership TEXT NOT NULL DEFAULT 'owned' "
                "CHECK(ownership IN ('owned', 'ku', 'need_to_purchase'))"
            )
        connection.execute(
            "UPDATE books SET current_page = total_pages "
            "WHERE status = 'finished' AND total_pages > 0 AND current_page = 0"
        )
        connection.execute(
            "UPDATE books SET current_page = CAST(ROUND(progress * total_pages / 100.0) AS INTEGER) "
            "WHERE status = 'in_progress' AND progress > 0 AND total_pages > 0 AND current_page = 0"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_books_status ON books(status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_books_title ON books(title)")


def clean_isbn(value: Any) -> str:
    return re.sub(r"[^0-9Xx]", "", str(value or "")).upper()[:13]


def normalize_cover(value: Any) -> str:
    cover = str(value or "").strip()
    if not cover:
        return ""
    if not cover.startswith("data:"):
        if len(cover) > 1000:
            raise ValueError("Cover URL is too long.")
        return cover

    match = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]+={0,2})", cover)
    if not match:
        raise ValueError("Uploaded cover must be a JPG, PNG, or WebP image.")
    image_type, encoded = match.groups()
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Uploaded cover data is invalid.") from exc
    if not content or len(content) > MAX_COVER_DATA_BYTES:
        raise ValueError("Uploaded cover must be smaller than 1 MB after compression.")
    signatures = {
        "jpeg": content.startswith(b"\xff\xd8\xff"),
        "png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    if not signatures[image_type]:
        raise ValueError("Uploaded cover does not match its image type.")
    return cover


def series_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if value else []


def split_series_label(value: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", value).strip(" ,;-–—")
    patterns = [
        r"^(.*?)\s*[,;-]?\s*\(\s*#\s*(\d+(?:\.\d+)?)\s*\)$",
        r"^(.*?)\s*[,;-]?\s*#\s*(\d+(?:\.\d+)?)$",
        r"^(.*?)\s*[,;-]?\s*(?:book|volume|vol\.?)[ ]*#?[ ]*(\d+(?:\.\d+)?)$",
        r"^(.*?)\s*,\s*(\d+(?:\.\d+)?)$",
        r"^(.*?)\s+(\d+(?:\.\d+)?)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, cleaned, re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip(" ,;-–—"), match.group(2)
    return cleaned, ""


def series_group_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    return re.sub(r"^the\s+", "", key)


def choose_series(candidates: list[tuple[str, bool]]) -> tuple[str, str]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for raw_value, exact in candidates:
        name, volume = split_series_label(raw_value)
        key = series_group_key(name)
        if not key or len(name) < 2:
            continue
        groups.setdefault(key, []).append({"name": name, "volume": volume, "exact": exact})
    if not groups:
        return "", ""

    def group_score(items: list[dict[str, Any]]) -> int:
        return sum(1 + (10 if item["exact"] else 0) + (2 if item["volume"] else 0) for item in items)

    best = max(groups.values(), key=group_score)
    display = max(
        best,
        key=lambda item: (item["exact"], item["name"].casefold().startswith("the "), len(item["name"])),
    )["name"]
    numbered = [item["volume"] for item in best if item["volume"]]
    volume = max(set(numbered), key=numbered.count) if numbered else ""
    return display, volume


def structured_series_position(value: Any, expected_series: str = "") -> str:
    """Read Open Library's newer work-level series position when unambiguous."""
    entries = value if isinstance(value, list) else []
    positions: list[str] = []
    expected_key = series_group_key(expected_series)
    for item in entries:
        if not isinstance(item, dict):
            continue
        series_ref = item.get("series") if isinstance(item.get("series"), dict) else {}
        named_series = str(series_ref.get("name") or item.get("name") or "").strip()
        if named_series and expected_key and series_group_key(named_series) != expected_key:
            continue
        position = str(item.get("position") or "").strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", position):
            positions.append(position)
    return positions[0] if len(set(positions)) == 1 else ""


def infer_volume_from_title(title: str, series: str) -> str:
    """Use an explicit numeric title suffix when source series labels omit position."""
    title_key = series_group_key(title)
    series_key = series_group_key(series)
    if not title_key or not series_key:
        return ""
    if title_key == series_key:
        return "1"
    if not title_key.startswith(f"{series_key} "):
        return ""
    suffix = title_key[len(series_key):].strip()
    match = re.match(r"(?:book\s+)?(\d+)(?:\s+(\d+))?(?:\s|$)", suffix)
    if not match:
        return ""
    return f"{match.group(1)}.{match.group(2)}" if match.group(2) else match.group(1)


def edition_isbn(edition: dict[str, Any]) -> str:
    for field in ("isbn_13", "isbn_10"):
        values = edition.get(field) or []
        if not isinstance(values, list):
            values = [values]
        for value in values:
            cleaned = clean_isbn(value)
            if len(cleaned) in {10, 13}:
                return cleaned
    return ""


def preferred_edition(entries: list[dict[str, Any]], preferred_cover_id: int = 0) -> dict[str, Any]:
    """Choose a usable English edition, preferring the cover shown in search results."""
    best: dict[str, Any] = {}
    best_score = -10_000
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        languages = entry.get("languages") or []
        language_keys = {
            str(item.get("key") or "") for item in languages if isinstance(item, dict)
        }
        covers = entry.get("covers") or []
        try:
            cover_ids = {int(value) for value in covers}
        except (TypeError, ValueError):
            cover_ids = set()
        physical_format = str(entry.get("physical_format") or "").casefold()
        score = 0
        if preferred_cover_id and preferred_cover_id in cover_ids:
            # Prefer the displayed cover, but not over an otherwise complete
            # English edition when the cover record is only a metadata stub.
            score += 35
        if "/languages/eng" in language_keys:
            score += 30
        elif language_keys:
            score -= 20
        if edition_isbn(entry):
            score += 18
        if entry.get("number_of_pages"):
            score += 10
        if cover_ids:
            score += 5
        if any(term in physical_format for term in ("audio", "cassette", "mp3", "cd")):
            score -= 35
        elif any(term in physical_format for term in ("paper", "hard", "mass market", "trade")):
            score += 8
        if score > best_score:
            best, best_score = entry, score
    return best


def external_text(url: str, timeout: int = 12) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "HomeLibraryV1/1.4"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError("Metadata page was too large.")
        return content.decode(response.headers.get_content_charset() or "utf-8", "replace")


def goodreads_text(url: str, timeout: int = 20) -> tuple[str, str]:
    """Read a public Goodreads page with the same headers as a normal browser.

    Goodreads no longer offers a public API. Its public book and series pages
    still expose Schema.org and React metadata, so this is deliberately isolated
    behind a best-effort adapter and every caller retains a fallback source.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/124.0 Safari/537.36 HomeLibraryV1/1.5"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError("Goodreads metadata page was too large.")
        page = content.decode(response.headers.get_content_charset() or "utf-8", "replace")
        return page, response.geturl()


def json_ld_documents(page: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    pattern = r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>"
    for raw in re.findall(pattern, page, flags=re.IGNORECASE | re.DOTALL):
        try:
            value = json.loads(html_lib.unescape(raw).strip())
        except json.JSONDecodeError:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, dict):
                continue
            documents.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                documents.extend(entry for entry in graph if isinstance(entry, dict))
    return documents


def goodreads_book_metadata(url: str) -> dict[str, Any]:
    """Return normalized metadata from a Goodreads book page or ISBN redirect."""
    requested_path = urllib.parse.urlparse(url).path
    requested_id_match = re.match(r"/book/show/(\d+)", requested_path)
    if requested_id_match and requested_id_match.group(1) in GOODREADS_BOOK_CACHE:
        return dict(GOODREADS_BOOK_CACHE[requested_id_match.group(1)])
    page, final_url = goodreads_text(url)
    parsed = urllib.parse.urlparse(final_url)
    if parsed.hostname != "www.goodreads.com" or not parsed.path.startswith("/book/show/"):
        return {}
    document = next(
        (item for item in json_ld_documents(page) if item.get("@type") == "Book"),
        None,
    )
    if not document:
        return {}

    series = ""
    volume = ""
    series_url = ""
    series_match = re.search(
        r'<a\s+href="(https://www\.goodreads\.com/series/[^"?#]+)"'
        r'\s+aria-label="Book\s+([^"<]+?)\s+in\s+the\s+(.+?)\s+series"',
        page,
        flags=re.IGNORECASE,
    )
    if series_match:
        series_url = html_lib.unescape(series_match.group(1)).strip()
        volume = html_lib.unescape(series_match.group(2)).strip()
        series = html_lib.unescape(series_match.group(3)).strip()

    title = html_lib.unescape(str(document.get("name") or "")).strip()
    title = re.sub(r"[\u200b-\u200d\ufeff]", "", title)
    if series and volume:
        suffix = re.compile(
            rf"\s*\(\s*{re.escape(series)}\s*(?:#|,\s*#?)\s*{re.escape(volume)}\s*\)\s*$",
            flags=re.IGNORECASE,
        )
        title = suffix.sub("", title).strip()

    raw_authors = document.get("author") or []
    if not isinstance(raw_authors, list):
        raw_authors = [raw_authors]
    authors = ", ".join(
        html_lib.unescape(str(item.get("name") or "")).strip()
        for item in raw_authors
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    )
    try:
        total_pages = max(0, min(100000, int(document.get("numberOfPages") or 0)))
    except (TypeError, ValueError):
        total_pages = 0
    isbn = clean_isbn(document.get("isbn"))
    if len(isbn) not in {10, 13}:
        isbn = ""
    book_format = str(document.get("bookFormat") or "").casefold()
    if any(term in book_format for term in ("audio", "cd", "cassette")):
        format_hint = "audiobook"
    elif any(term in book_format for term in ("ebook", "e-book", "kindle", "epub", "electronic")):
        format_hint = "ebook"
    elif book_format:
        format_hint = "physical"
    else:
        format_hint = ""
    published_match = re.search(
        r'data-testid="publicationInfo"[^>]*>.*?\b((?:19|20)\d{2})\b',
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )
    book_id_match = re.match(r"/book/show/(\d+)", parsed.path)
    result = {
        "title": title,
        "authors": authors,
        "year": int(published_match.group(1)) if published_match else None,
        "isbn": isbn,
        "cover_url": str(document.get("image") or "").strip(),
        "total_pages": total_pages,
        "series": series,
        "volume": volume if re.fullmatch(r"\d+(?:\.\d+)?", volume) else "",
        "series_url": series_url,
        "format_hint": format_hint,
        "goodreads_url": final_url,
        "goodreads_id": book_id_match.group(1) if book_id_match else "",
    }
    if result["goodreads_id"]:
        GOODREADS_BOOK_CACHE[result["goodreads_id"]] = dict(result)
    return result


def enrich_goodreads_book(book: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(book)
    detail_url = str(enriched.pop("_detail_url", ""))
    if not detail_url.startswith("https://www.goodreads.com/book/show/"):
        return enriched
    try:
        detail = goodreads_book_metadata(detail_url)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        detail = {}
    for field in ("title", "authors", "year", "isbn", "cover_url", "total_pages"):
        if detail.get(field):
            enriched[field] = detail[field]
    return enriched


def goodreads_series_books(series_url: str, requested_series: str) -> list[dict[str, Any]]:
    """Read ordered books from Goodreads' public series page metadata."""
    parsed = urllib.parse.urlparse(series_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.goodreads.com"
        or not re.fullmatch(r"/series/\d+-[a-z0-9-]+", parsed.path)
    ):
        return []
    cache_key = f"{parsed.path}|{series_group_key(requested_series)}"
    if cache_key in GOODREADS_SERIES_CACHE:
        return [dict(book) for book in GOODREADS_SERIES_CACHE[cache_key]]
    try:
        page, _ = goodreads_text(series_url)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []

    heading_match = re.search(
        r'<h1[^>]*class="[^"]*gr-h1[^"]*"[^>]*>(.*?)</h1>',
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )
    heading = html_lib.unescape(re.sub(r"<[^>]+>", "", heading_match.group(1))).strip() if heading_match else ""
    source_series = re.sub(r"\s+Series\s*$", "", heading, flags=re.IGNORECASE).strip()
    if source_series and series_group_key(source_series) != series_group_key(requested_series):
        return []

    books: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_positions: set[str] = set()
    props_pattern = r'data-react-class="ReactComponents\.SeriesList"\s+data-react-props="([^"]+)"'
    for encoded_props in re.findall(props_pattern, page, flags=re.IGNORECASE):
        try:
            props = json.loads(html_lib.unescape(encoded_props))
        except json.JSONDecodeError:
            continue
        entries = props.get("series") or []
        headers = props.get("seriesHeaders") or []
        if not isinstance(entries, list) or not isinstance(headers, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            item = entry.get("book")
            if not isinstance(item, dict):
                continue
            book_id = str(item.get("bookId") or "").strip()
            title = html_lib.unescape(str(item.get("bookTitleBare") or item.get("title") or "")).strip()
            if not book_id or book_id in seen_ids or not title or looks_like_collection(title):
                continue
            header = html_lib.unescape(str(headers[index] if index < len(headers) else ""))
            position_match = re.search(r"\bBook\s+(\d+(?:\.\d+)?)\b", header, flags=re.IGNORECASE)
            if not position_match:
                continue
            position = position_match.group(1)
            if position in seen_positions:
                continue
            title = re.sub(
                rf"\s*\(\s*{re.escape(source_series or requested_series)}\s*"
                rf"(?:#|,\s*#?)\s*{re.escape(position)}\s*\)\s*$",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip()
            title = re.sub(r"[\u200b-\u200d\ufeff]", "", title)
            seen_ids.add(book_id)
            seen_positions.add(position)
            author_item = item.get("author") if isinstance(item.get("author"), dict) else {}
            display_author = html_lib.unescape(str(author_item.get("name") or "")).strip()
            cover_url = html_lib.unescape(str(item.get("imageUrl") or "")).strip()
            cover_url = re.sub(r"\._S[A-Z]\d+_\.(?=[A-Za-z]+$)", ".", cover_url)
            detail_path = html_lib.unescape(str(item.get("bookUrl") or "")).strip()
            detail_url = urllib.parse.urljoin("https://www.goodreads.com", detail_path)
            publication = str(item.get("publicationDate") or "")
            year_match = re.search(r"\b((?:19|20)\d{2})\b", publication)
            try:
                total_pages = max(0, min(100000, int(item.get("numPages") or 0)))
            except (TypeError, ValueError):
                total_pages = 0
            books.append(
                {
                    "title": title,
                    "authors": display_author,
                    "year": int(year_match.group(1)) if year_match else None,
                    "isbn": "",
                    "cover_url": cover_url,
                    "open_library_key": f"goodreads:{book_id}",
                    "total_pages": total_pages,
                    "series": source_series or requested_series,
                    "volume": position,
                    "status": "tbr",
                    "formats": ["physical"],
                    "_detail_url": detail_url,
                }
            )
    if books:
        with ThreadPoolExecutor(max_workers=min(5, len(books))) as executor:
            books = list(executor.map(enrich_goodreads_book, books))
        GOODREADS_SERIES_CACHE[cache_key] = [dict(book) for book in books]
    return books


def aethon_series_books(series: str, author: str) -> list[dict[str, Any]]:
    """Read Aethon's public Schema.org series feed when this is one of its titles."""
    slug = re.sub(r"[^a-z0-9]+", "-", series.casefold()).strip("-")
    if not slug:
        return []
    try:
        page = external_text(f"https://aethonbooks.com/book-series/{slug}/")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    target_key = series_group_key(series)
    requested_author = series_group_key(author)
    series_doc = next(
        (
            item for item in json_ld_documents(page)
            if item.get("@type") == "CreativeWorkSeries"
            and series_group_key(str(item.get("name") or "")) == target_key
        ),
        None,
    )
    if not series_doc:
        return []
    source_authors = series_doc.get("author") or []
    if not isinstance(source_authors, list):
        source_authors = [source_authors]
    author_names = [
        str(item.get("name") or "").strip() for item in source_authors if isinstance(item, dict)
    ]
    if requested_author and author_names and requested_author not in {
        series_group_key(name) for name in author_names
    }:
        return []
    display_author = ", ".join(name for name in author_names if name) or author
    parts = series_doc.get("hasPart") or []
    if not isinstance(parts, list):
        parts = [parts]
    books: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in parts:
        if not isinstance(part, dict) or part.get("@type") != "Book":
            continue
        title = str(part.get("name") or "").strip()
        detail_url = str(part.get("url") or "").strip()
        identity = f"{title.casefold()}|{display_author.casefold()}"
        if not title or looks_like_collection(title) or identity in seen:
            continue
        seen.add(identity)
        published = str(part.get("datePublished") or "")
        position = str(part.get("position") or "").strip()
        books.append(
            {
                "title": title,
                "authors": display_author,
                "year": int(published[:4]) if re.match(r"^\d{4}", published) else None,
                "isbn": "",
                "cover_url": str(part.get("image") or "").strip(),
                "open_library_key": "",
                "total_pages": 0,
                "series": str(series_doc.get("name") or series),
                "volume": position if re.fullmatch(r"\d+(?:\.\d+)?", position) else "",
                "status": "tbr",
                "formats": ["physical"],
                "_detail_url": detail_url,
            }
        )
    return books


def enrich_aethon_book(book: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(book)
    detail_url = str(enriched.pop("_detail_url", ""))
    if not detail_url.startswith("https://aethonbooks.com/book/"):
        return enriched
    try:
        page = external_text(detail_url)
        document = next(
            (item for item in json_ld_documents(page) if item.get("@type") == "Book"),
            None,
        )
    except (urllib.error.URLError, TimeoutError, ValueError):
        document = None
    if not document:
        return enriched
    isbn = clean_isbn(document.get("isbn"))
    if len(isbn) in {10, 13}:
        enriched["isbn"] = isbn
    try:
        enriched["total_pages"] = max(0, min(100000, int(document.get("numberOfPages") or 0)))
    except (TypeError, ValueError):
        pass
    image = str(document.get("image") or "").strip()
    if image.startswith("https://"):
        enriched["cover_url"] = image
    return enriched


def open_library_json(url: str, timeout: int = 10) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "HomeLibraryV1/1.3"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def looks_like_collection(title: str) -> bool:
    lowered = title.casefold()
    phrases = (
        "box set",
        "boxed set",
        "collection set",
        "complete collection",
        "complete series",
        "series collection",
        "books collection",
        "set of ",
        "bundle",
        "coloring book",
        "tarot",
    )
    return any(phrase in lowered for phrase in phrases) or bool(
        re.search(r"\b(?:books?|series)\s*\d+\s*[-–]\s*\d+\b", lowered)
    )


def allowed_cover_host(hostname: str | None) -> bool:
    host = (hostname or "").casefold()
    # Open Library redirects older cover IDs to its storage on Archive.org.
    return (
        host in {
            "covers.openlibrary.org",
            "aethonbooks.com",
            "i.gr-assets.com",
            "m.media-amazon.com",
            "archive.org",
        }
        or host.endswith(".archive.org")
        or host.endswith(".smushcdn.com")
    )


def goodreads_book_id(value: str) -> str:
    """Extract a Goodreads book ID without accepting an arbitrary fetch URL."""
    raw_value = str(value or "").strip()
    key_match = re.fullmatch(r"goodreads:(\d{1,20})", raw_value, flags=re.IGNORECASE)
    if key_match:
        return key_match.group(1)
    try:
        parsed = urllib.parse.urlparse(raw_value)
    except ValueError:
        return ""
    if not allowed_cover_host(parsed.hostname):
        return ""
    cover_match = re.search(
        r"/books/\d+[a-z]/(\d{1,20})\.(?:jpe?g|png|webp)$",
        parsed.path,
        flags=re.IGNORECASE,
    )
    return cover_match.group(1) if cover_match else ""


def normalize_book(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    current = existing or {}

    def text_field(name: str, limit: int) -> str:
        value = payload[name] if name in payload else current.get(name, "")
        return str(value or "").strip()[:limit]

    title = text_field("title", 300)
    if not title:
        raise ValueError("A title is required.")

    status = text_field("status", 20) or "tbr"
    if status not in STATUSES:
        raise ValueError("Status must be TBR, In Progress, Finished, or DNF.")

    ownership = text_field("ownership", 30) or "owned"
    if ownership not in OWNERSHIP_STATES:
        raise ValueError("Ownership must be Owned, Kindle Unlimited, or Need to Purchase.")

    try:
        progress = int(payload.get("progress", current.get("progress", 0)) or 0)
        current_page = int(payload.get("current_page", current.get("current_page", 0)) or 0)
        rating = int(payload.get("rating", current.get("rating", 0)) or 0)
        total_pages = int(payload.get("total_pages", current.get("total_pages", 0)) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Current page, rating, and total pages must be numbers.") from exc

    progress = max(0, min(100, progress))
    current_page = max(0, min(100000, current_page))
    rating = max(0, min(5, rating))
    total_pages = max(0, min(100000, total_pages))
    if status == "finished":
        progress = 100
        if total_pages:
            current_page = total_pages
    elif status == "tbr":
        progress = 0
        current_page = 0
    elif total_pages:
        current_page = min(current_page, total_pages)
        progress = round(current_page / total_pages * 100)
    else:
        current_page = 0
        progress = 0

    raw_formats = payload.get("formats", current.get("formats", ["physical"] if existing is None else []))
    if isinstance(raw_formats, str):
        try:
            raw_formats = json.loads(raw_formats)
        except json.JSONDecodeError:
            raw_formats = [part.strip() for part in raw_formats.split(",")]
    if not isinstance(raw_formats, list):
        raw_formats = []
    allowed_formats = {"physical", "ebook", "audiobook"}
    formats = []
    for value in raw_formats:
        normalized = str(value).strip().lower()
        if normalized in allowed_formats and normalized not in formats:
            formats.append(normalized)

    started_at = text_field("started_at", 10)
    finished_at = text_field("finished_at", 10)
    today = date.today().isoformat()
    previous_status = current.get("status", "")
    if status == "in_progress" and not started_at:
        started_at = today
    if status == "dnf" and not started_at:
        started_at = today
    if status == "finished" and not finished_at:
        finished_at = today
    if status != "finished" and previous_status == "finished":
        finished_at = ""
    if status != previous_status and status == "tbr":
        started_at = ""
        finished_at = ""

    isbn = clean_isbn(payload.get("isbn", current.get("isbn", "")))
    cover_url = normalize_cover(payload.get("cover_url", current.get("cover_url", "")))
    if not cover_url and isbn:
        cover_url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"

    return {
        "title": title,
        "authors": text_field("authors", 500),
        "isbn": isbn,
        "cover_url": cover_url,
        "open_library_key": text_field("open_library_key", 200),
        "status": status,
        "progress": progress,
        "current_page": current_page,
        "rating": rating,
        "series": text_field("series", 300),
        "volume": text_field("volume", 50),
        "total_pages": total_pages,
        "formats": json.dumps(formats, separators=(",", ":")),
        "ownership": ownership,
        "notes": text_field("notes", 5000),
        "started_at": started_at,
        "finished_at": finished_at,
    }


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = {key: row[key] for key in row.keys()}
    if "formats" in result:
        try:
            result["formats"] = json.loads(result["formats"] or "[]")
        except (json.JSONDecodeError, TypeError):
            result["formats"] = []
    return result


class LibraryHandler(BaseHTTPRequestHandler):
    server_version = "HomeLibrary/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload: Any, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request size.") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("Request body is empty or too large.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            return self.send_json({"status": "ok", "database": DB_PATH.name})
        if parsed.path == "/api/books":
            return self.list_books(urllib.parse.parse_qs(parsed.query))
        if parsed.path == "/api/stats":
            return self.get_stats()
        if parsed.path == "/api/lookup":
            return self.lookup_books(urllib.parse.parse_qs(parsed.query))
        if parsed.path == "/api/lookup/details":
            return self.lookup_book_details(urllib.parse.parse_qs(parsed.query))
        if parsed.path == "/api/lookup/series":
            return self.lookup_series(urllib.parse.parse_qs(parsed.query))
        if parsed.path == "/api/cover":
            return self.proxy_cover(urllib.parse.parse_qs(parsed.query))
        if parsed.path == "/api/export":
            return self.export_books()

        match = re.fullmatch(r"/api/books/(\d+)", parsed.path)
        if match:
            return self.get_book(int(match.group(1)))
        if parsed.path.startswith("/api/"):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found.")
        self.serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/import":
            return self.import_books()
        if parsed.path == "/api/books/series":
            return self.create_series_books()
        if parsed.path == "/api/books":
            return self.create_book()
        self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found.")

    def do_PUT(self) -> None:  # noqa: N802
        match = re.fullmatch(r"/api/books/(\d+)", urllib.parse.urlparse(self.path).path)
        if match:
            return self.update_book(int(match.group(1)))
        self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found.")

    def do_DELETE(self) -> None:  # noqa: N802
        match = re.fullmatch(r"/api/books/(\d+)", urllib.parse.urlparse(self.path).path)
        if match:
            return self.delete_book(int(match.group(1)))
        self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found.")

    def list_books(self, query: dict[str, list[str]]) -> None:
        search = (query.get("q", [""])[0] or "").strip()
        status = (query.get("status", ["all"])[0] or "all").strip()
        ownership = (query.get("ownership", ["all"])[0] or "all").strip()
        sort = (query.get("sort", ["updated"])[0] or "updated").strip()
        clauses: list[str] = []
        params: list[Any] = []
        if search:
            clauses.append("(title LIKE ? OR authors LIKE ? OR series LIKE ? OR isbn LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        if status in STATUSES:
            clauses.append("status = ?")
            params.append(status)
        if ownership in OWNERSHIP_STATES:
            clauses.append("ownership = ?")
            params.append(ownership)

        order_by = {
            "title": "title COLLATE NOCASE ASC",
            "author": "authors COLLATE NOCASE ASC, title COLLATE NOCASE ASC",
            "oldest": "created_at ASC",
            "updated": "updated_at DESC",
        }.get(sort, "updated_at DESC")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with db_connection() as connection:
            rows = connection.execute(f"SELECT * FROM books {where} ORDER BY {order_by}", params).fetchall()
        self.send_json({"books": [row_to_dict(row) for row in rows], "count": len(rows)})

    def get_stats(self) -> None:
        with db_connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'tbr' THEN 1 ELSE 0 END) AS tbr,
                       SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
                       SUM(CASE WHEN status = 'finished' THEN 1 ELSE 0 END) AS finished,
                       SUM(CASE WHEN status = 'dnf' THEN 1 ELSE 0 END) AS dnf
                FROM books
                """
            ).fetchone()
        stats = {key: int(row[key] or 0) for key in row.keys()}
        self.send_json(stats)

    def get_book(self, book_id: int) -> None:
        with db_connection() as connection:
            row = connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Book not found.")
        self.send_json(row_to_dict(row))

    def create_book(self) -> None:
        try:
            book = normalize_book(self.read_json())
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        timestamp = now_iso()
        columns = list(book.keys()) + ["created_at", "updated_at"]
        values = list(book.values()) + [timestamp, timestamp]
        placeholders = ", ".join("?" for _ in values)
        with db_connection() as connection:
            cursor = connection.execute(
                f"INSERT INTO books ({', '.join(columns)}) VALUES ({placeholders})", values
            )
            row = connection.execute("SELECT * FROM books WHERE id = ?", (cursor.lastrowid,)).fetchone()
        self.send_json(row_to_dict(row), HTTPStatus.CREATED)

    def update_book(self, book_id: int) -> None:
        with db_connection() as connection:
            row = connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
            if not row:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "Book not found.")
            try:
                book = normalize_book(self.read_json(), row_to_dict(row))
            except ValueError as exc:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            assignments = ", ".join(f"{key} = ?" for key in book)
            connection.execute(
                f"UPDATE books SET {assignments}, updated_at = ? WHERE id = ?",
                [*book.values(), now_iso(), book_id],
            )
            updated = connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        self.send_json(row_to_dict(updated))

    def delete_book(self, book_id: int) -> None:
        with db_connection() as connection:
            cursor = connection.execute("DELETE FROM books WHERE id = ?", (book_id,))
        if not cursor.rowcount:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Book not found.")
        self.send_json({"deleted": True, "id": book_id})

    def lookup_books(self, query: dict[str, list[str]]) -> None:
        search = (query.get("q", [""])[0] or "").strip()
        if len(search) < 2:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "Enter at least 2 characters.")

        compact = clean_isbn(search)
        is_isbn = len(compact) in {10, 13} and len(re.sub(r"[^0-9Xx]", "", search)) == len(compact)
        params = {
            "limit": "12",
            "fields": "key,title,author_name,first_publish_year,isbn,cover_i,edition_count,number_of_pages_median",
        }
        params["isbn" if is_isbn else "q"] = compact if is_isbn else search
        url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "HomeLibraryV1/1.0 (personal library catalog)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"Open Library lookup failed: {exc}")
            return self.send_error_json(
                HTTPStatus.BAD_GATEWAY,
                "Open Library could not be reached. You can still enter the book manually.",
            )

        results = []
        seen: set[tuple[str, str]] = set()
        for item in data.get("docs", []):
            title = str(item.get("title") or "").strip()
            authors = ", ".join(item.get("author_name", [])[:3])
            if not title or (title.casefold(), authors.casefold()) in seen:
                continue
            seen.add((title.casefold(), authors.casefold()))
            isbns = [clean_isbn(value) for value in item.get("isbn", [])]
            isbn = next((value for value in isbns if len(value) == 13), isbns[0] if isbns else "")
            cover_id = item.get("cover_i")
            cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else ""
            results.append(
                {
                    "title": title,
                    "authors": authors,
                    "year": item.get("first_publish_year"),
                    # A work-level title search can contain ISBNs from many languages and
                    # formats. Only claim an exact ISBN when that is what the user searched.
                    "isbn": isbn if is_isbn else "",
                    "exact_edition": is_isbn,
                    "cover_url": cover_url,
                    "cover_id": cover_id or 0,
                    "open_library_key": item.get("key", ""),
                    "total_pages": item.get("number_of_pages_median") or 0,
                }
            )
        self.send_json({"results": results, "count": len(results)})

    def lookup_book_details(self, query: dict[str, list[str]]) -> None:
        isbn = clean_isbn((query.get("isbn", [""])[0] or "").strip())
        work_key = (query.get("work_key", [""])[0] or "").strip()
        cover_url = (query.get("cover_url", [""])[0] or "").strip()[:2000]
        goodreads_id = goodreads_book_id(work_key) or goodreads_book_id(cover_url)
        try:
            preferred_cover_id = int((query.get("cover_id", ["0"])[0] or "0").strip())
        except ValueError:
            preferred_cover_id = 0
        if work_key and not re.fullmatch(r"/works/OL\d+W", work_key):
            work_key = ""
        if not isbn and not work_key and not goodreads_id:
            return self.send_error_json(
                HTTPStatus.BAD_REQUEST,
                "An ISBN, catalog key, or Goodreads cover is required.",
            )

        if goodreads_id:
            try:
                direct_goodreads = goodreads_book_metadata(
                    f"https://www.goodreads.com/book/show/{goodreads_id}"
                )
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                print(f"Goodreads book ID lookup failed: {exc}")
                direct_goodreads = {}
            if direct_goodreads:
                return self.send_json(
                    {
                        "series": str(direct_goodreads.get("series") or ""),
                        "volume": str(direct_goodreads.get("volume") or ""),
                        "isbn": str(direct_goodreads.get("isbn") or isbn),
                        "total_pages": int(direct_goodreads.get("total_pages") or 0),
                        "format_hint": str(direct_goodreads.get("format_hint") or ""),
                        "title": str(direct_goodreads.get("title") or ""),
                        "authors": str(direct_goodreads.get("authors") or ""),
                        "cover_url": str(direct_goodreads.get("cover_url") or cover_url),
                        "series_url": str(direct_goodreads.get("series_url") or ""),
                        "source": "goodreads",
                        "warnings": [],
                    }
                )

        def fetch_json(url: str, timeout: int = 10) -> dict[str, Any]:
            request = urllib.request.Request(url, headers={"User-Agent": "HomeLibraryV1/1.2"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)

        edition: dict[str, Any] = {}
        edition_entries: list[dict[str, Any]] = []
        series_candidates: list[tuple[str, bool]] = []
        warnings: list[str] = []
        if isbn:
            try:
                edition = fetch_json(f"https://openlibrary.org/isbn/{urllib.parse.quote(isbn)}.json")
                series_candidates.extend((value, True) for value in series_values(edition.get("series")))
                if not work_key:
                    works = edition.get("works") or []
                    if works and isinstance(works[0], dict):
                        candidate_key = str(works[0].get("key") or "")
                        if re.fullmatch(r"/works/OL\d+W", candidate_key):
                            work_key = candidate_key
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"Open Library edition lookup failed: {exc}")
                warnings.append("Exact edition details were unavailable.")

        work: dict[str, Any] = {}
        if work_key:
            try:
                editions_url = f"https://openlibrary.org{work_key}/editions.json?limit=100"
                editions = fetch_json(editions_url, timeout=12)
                edition_entries = [item for item in editions.get("entries", []) if isinstance(item, dict)]
                for item in edition_entries:
                    series_candidates.extend((value, False) for value in series_values(item.get("series")))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"Open Library series lookup failed: {exc}")
                warnings.append("Series details were unavailable.")

            try:
                work = fetch_json(f"https://openlibrary.org{work_key}.json", timeout=10)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"Open Library work lookup failed: {exc}")

        if not edition and edition_entries:
            edition = preferred_edition(edition_entries, preferred_cover_id)

        series, volume = choose_series(series_candidates)
        if series and not volume:
            volume = structured_series_position(work.get("series"), series)
        if series and not volume:
            volume = infer_volume_from_title(str(work.get("title") or edition.get("title") or ""), series)
        correction = WORK_METADATA_CORRECTIONS.get(work_key, {})
        corrected_series = correction.get("series", "")
        if corrected_series and (not series or series_group_key(corrected_series) == series_group_key(series)):
            series = corrected_series
            volume = correction.get("volume", "") or volume
        try:
            total_pages = int(edition.get("number_of_pages") or 0)
        except (TypeError, ValueError):
            total_pages = 0
        total_pages = max(0, min(100000, total_pages))

        physical_format = str(edition.get("physical_format") or "").casefold()
        format_hint = ""
        if any(term in physical_format for term in ("audio", "cd", "cassette")):
            format_hint = "audiobook"
        elif any(term in physical_format for term in ("ebook", "e-book", "kindle", "epub", "electronic")):
            format_hint = "ebook"
        elif physical_format:
            format_hint = "physical"

        resolved_isbn = edition_isbn(edition) or isbn
        goodreads: dict[str, Any] = {}
        if goodreads_id:
            try:
                goodreads = goodreads_book_metadata(
                    f"https://www.goodreads.com/book/show/{goodreads_id}"
                )
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                print(f"Goodreads book ID lookup failed: {exc}")
        if not goodreads and resolved_isbn:
            try:
                goodreads = goodreads_book_metadata(
                    f"https://www.goodreads.com/book/isbn/{urllib.parse.quote(resolved_isbn)}"
                )
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                print(f"Goodreads book lookup failed: {exc}")
        if goodreads:
            series = str(goodreads.get("series") or series)
            volume = str(goodreads.get("volume") or volume)
            resolved_isbn = str(goodreads.get("isbn") or resolved_isbn)
            total_pages = int(goodreads.get("total_pages") or total_pages)
            format_hint = str(goodreads.get("format_hint") or format_hint)

        self.send_json(
            {
                "series": series,
                "volume": volume,
                "isbn": resolved_isbn,
                "total_pages": total_pages,
                "format_hint": format_hint,
                "title": str(goodreads.get("title") or ""),
                "authors": str(goodreads.get("authors") or ""),
                "cover_url": str(goodreads.get("cover_url") or ""),
                "series_url": str(goodreads.get("series_url") or ""),
                "source": "goodreads" if goodreads else "open_library",
                "warnings": warnings,
            }
        )

    def send_series_discovery(self, requested_series: str, discovered: list[dict[str, Any]]) -> None:
        with db_connection() as connection:
            existing_rows = connection.execute(
                "SELECT title, authors, isbn, open_library_key FROM books"
            ).fetchall()
        existing_keys = {row["open_library_key"] for row in existing_rows if row["open_library_key"]}
        existing_isbns = {row["isbn"] for row in existing_rows if row["isbn"]}
        existing_titles = {
            (row["title"].strip().casefold(), row["authors"].strip().casefold()) for row in existing_rows
        }

        unique: dict[str, dict[str, Any]] = {}
        for book in discovered:
            volume = str(book.get("volume") or "").strip()
            identity = (
                f"volume:{volume}"
                if re.fullmatch(r"\d+(?:\.\d+)?", volume)
                else f"{series_group_key(book['title'])}|{series_group_key(book['authors'])}"
            )
            if identity in unique:
                current = unique[identity]
                for field in ("isbn", "cover_url", "open_library_key", "volume", "year"):
                    if not current.get(field) and book.get(field):
                        current[field] = book[field]
                if not current.get("total_pages") and book.get("total_pages"):
                    current["total_pages"] = book["total_pages"]
                continue
            unique[identity] = book

        # A catalog can occasionally reuse a placeholder ISBN on an announced
        # title. Preserve the earliest book and suppress later conflicts.
        isbn_owners: dict[str, dict[str, Any]] = {}
        for book in sorted(unique.values(), key=lambda item: (item.get("year") or 9999, item["title"])):
            isbn = str(book.get("isbn") or "")
            if not isbn:
                continue
            if isbn in isbn_owners:
                book["isbn"] = ""
            else:
                isbn_owners[isbn] = book

        existing_count = 0
        for book in unique.values():
            is_existing = (
                (book["open_library_key"] and book["open_library_key"] in existing_keys)
                or (book["isbn"] and book["isbn"] in existing_isbns)
                or (book["title"].casefold(), book["authors"].casefold()) in existing_titles
            )
            book["existing"] = bool(is_existing)
            if is_existing:
                existing_count += 1

        def sort_key(book: dict[str, Any]) -> tuple[float, str]:
            try:
                order = float(book.get("volume") or "inf")
            except (ValueError, TypeError):
                order = float("inf")
            return order, book["title"].casefold()

        books = sorted(unique.values(), key=sort_key)
        self.send_json(
            {
                "series": requested_series,
                "books": books,
                "missing": [book for book in books if not book["existing"]],
                "existing_count": existing_count,
                "discovered_count": len(books),
            }
        )

    def lookup_series(self, query: dict[str, list[str]]) -> None:
        requested_series = (query.get("series", [""])[0] or "").strip()[:300]
        author = (query.get("author", [""])[0] or "").strip()[:300]
        exclude_key = (query.get("exclude_key", [""])[0] or "").strip()
        exclude_title = (query.get("exclude_title", [""])[0] or "").strip()[:300].casefold()
        exclude_volume = (query.get("exclude_volume", [""])[0] or "").strip()[:20]
        target_key = series_group_key(requested_series)
        if len(target_key) < 2:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "A series name is required.")

        # Goodreads is the primary series catalog when the selected book linked
        # us to an exact series page. Open Library and publisher feeds remain the
        # fallback when that public page is unavailable or changes structure.
        series_url = (query.get("series_url", [""])[0] or "").strip()
        if series_url:
            goodreads_candidates = [
                book for book in goodreads_series_books(series_url, requested_series)
                if book["title"].casefold() != exclude_title
                and (not exclude_volume or str(book.get("volume") or "") != exclude_volume)
            ]
            if goodreads_candidates:
                # An official publisher feed can fill a missing ISBN or page count,
                # but Goodreads remains first in the merge and therefore owns the
                # title, series order, author label, and cover choice.
                publisher_candidates = aethon_series_books(requested_series, author)
                if publisher_candidates:
                    with ThreadPoolExecutor(max_workers=min(5, len(publisher_candidates))) as executor:
                        supplements = list(executor.map(enrich_aethon_book, publisher_candidates))
                    goodreads_candidates.extend(
                        book for book in supplements
                        if book["title"].casefold() != exclude_title
                        and (not exclude_volume or str(book.get("volume") or "") != exclude_volume)
                    )
                return self.send_series_discovery(requested_series, goodreads_candidates)

        safe_series = requested_series.replace('"', " ")
        safe_author = author.replace('"', " ")
        search_query = f'series:"{safe_series}"'
        if safe_author:
            search_query += f' AND author:"{safe_author}"'
        params = {
            "q": search_query,
            "limit": "30",
            "fields": "key,title,author_name,first_publish_year,isbn,cover_i,number_of_pages_median",
        }
        url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
        try:
            data = open_library_json(url, timeout=12)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"Open Library series search failed: {exc}")
            return self.send_error_json(HTTPStatus.BAD_GATEWAY, "The full series could not be discovered right now.")

        raw_candidates: list[dict[str, Any]] = []
        candidate_keys: set[str] = set()

        def collect_candidates(items: list[dict[str, Any]], limit: int) -> None:
            for item in items:
                title = str(item.get("title") or "").strip()
                work_key = str(item.get("key") or "")
                if (
                    not title
                    or looks_like_collection(title)
                    or not re.fullmatch(r"/works/OL\d+W", work_key)
                    or work_key == exclude_key
                    or work_key in candidate_keys
                ):
                    continue
                candidate_keys.add(work_key)
                raw_candidates.append(item)
                if len(raw_candidates) >= limit:
                    return

        collect_candidates(data.get("docs", []), 20)

        # Open Library has two generations of series metadata. Some newer books
        # validate at the edition/work level but are absent from its series search
        # index. When the indexed search is sparse, scan the author's catalog and
        # keep only candidates whose edition metadata validates the requested series.
        if len(raw_candidates) < 2 and safe_author:
            fallback_params = {
                "q": f'author:"{safe_author}"',
                "limit": "50",
                "fields": "key,title,author_name,first_publish_year,isbn,cover_i,number_of_pages_median",
            }
            fallback_url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(fallback_params)
            try:
                fallback_data = open_library_json(fallback_url, timeout=12)
                collect_candidates(fallback_data.get("docs", []), 40)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"Open Library author fallback failed: {exc}")

        def enrich(item: dict[str, Any]) -> dict[str, Any] | None:
            work_key = str(item.get("key") or "")
            try:
                editions = open_library_json(f"https://openlibrary.org{work_key}/editions.json?limit=100", timeout=10)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                return None
            edition_entries = [entry for entry in editions.get("entries", []) if isinstance(entry, dict)]
            labels: list[tuple[str, bool]] = []
            for edition in edition_entries:
                labels.extend((value, False) for value in series_values(edition.get("series")))
            found_series, volume = choose_series(labels)
            correction = WORK_METADATA_CORRECTIONS.get(work_key, {})
            corrected_series = correction.get("series", "")
            if corrected_series and (
                not found_series or series_group_key(corrected_series) == series_group_key(found_series)
            ):
                found_series = corrected_series
                volume = correction.get("volume", "") or volume
            if series_group_key(found_series) != target_key:
                return None
            if not volume:
                try:
                    work = open_library_json(f"https://openlibrary.org{work_key}.json", timeout=10)
                    volume = structured_series_position(work.get("series"), found_series)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                    pass
            title = str(item.get("title") or "").strip()
            if not volume:
                volume = infer_volume_from_title(title, found_series)
            cover_id = item.get("cover_i")
            edition = preferred_edition(edition_entries, int(cover_id or 0))
            try:
                edition_pages = int(edition.get("number_of_pages") or 0)
            except (TypeError, ValueError):
                edition_pages = 0
            return {
                "title": title,
                "authors": ", ".join(item.get("author_name", [])[:3]),
                "year": item.get("first_publish_year"),
                "isbn": edition_isbn(edition),
                "cover_url": f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else "",
                "open_library_key": work_key,
                "total_pages": edition_pages or item.get("number_of_pages_median") or 0,
                "series": found_series or requested_series,
                "volume": volume,
                "status": "tbr",
                "formats": ["physical"],
            }

        discovered: list[dict[str, Any]] = []
        if raw_candidates:
            with ThreadPoolExecutor(max_workers=min(8, len(raw_candidates))) as executor:
                futures = [executor.submit(enrich, item) for item in raw_candidates]
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as exc:  # Keep one malformed edition from blocking the series.
                        print(f"Open Library series candidate failed: {exc}")
                        result = None
                    if result:
                        discovered.append(result)

        # Merge an official publisher feed when the series belongs to Aethon.
        # Its Schema.org catalog includes announced books that Open Library may not
        # have ingested yet, plus publisher ISBNs, covers, positions, and page counts.
        open_library_titles = {
            (book["title"].casefold(), series_group_key(book["authors"])): book for book in discovered
        }
        publisher_candidates = [
            book for book in aethon_series_books(requested_series, author)
            if book["title"].casefold() != exclude_title
            and (
                (book["title"].casefold(), series_group_key(book["authors"])) not in open_library_titles
                or not open_library_titles[
                    (book["title"].casefold(), series_group_key(book["authors"]))
                ].get("isbn")
                or not open_library_titles[
                    (book["title"].casefold(), series_group_key(book["authors"]))
                ].get("total_pages")
            )
        ]
        if publisher_candidates:
            with ThreadPoolExecutor(max_workers=min(5, len(publisher_candidates))) as executor:
                futures = [executor.submit(enrich_aethon_book, book) for book in publisher_candidates]
                for future in as_completed(futures):
                    try:
                        discovered.append(future.result())
                    except Exception as exc:
                        print(f"Aethon series candidate failed: {exc}")

        return self.send_series_discovery(requested_series, discovered)

    def create_series_books(self) -> None:
        try:
            payload = self.read_json()
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        raw_books = payload.get("books")
        if not isinstance(raw_books, list) or len(raw_books) > 50:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "Series books must be a list of at most 50 items.")
        inherited_formats = payload.get("formats") if "formats" in payload else None
        if inherited_formats is not None and (
            not isinstance(inherited_formats, list) or len(inherited_formats) > 3
        ):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "Series formats must be a list.")
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        errors: list[str] = []
        with db_connection() as connection:
            rows = connection.execute("SELECT title, authors, isbn, open_library_key FROM books").fetchall()
            existing_keys = {row["open_library_key"] for row in rows if row["open_library_key"]}
            existing_isbns = {row["isbn"] for row in rows if row["isbn"]}
            existing_titles = {(row["title"].strip().casefold(), row["authors"].strip().casefold()) for row in rows}

            for item in raw_books:
                if not isinstance(item, dict):
                    continue
                candidate = dict(item)
                candidate["status"] = "tbr"
                candidate["current_page"] = 0
                if inherited_formats is not None:
                    candidate["formats"] = inherited_formats
                else:
                    candidate.setdefault("formats", ["physical"])
                candidate["ownership"] = "need_to_purchase"
                try:
                    book = normalize_book(candidate)
                except ValueError as exc:
                    errors.append(str(exc))
                    continue
                title_key = (book["title"].casefold(), book["authors"].casefold())
                if (
                    (book["open_library_key"] and book["open_library_key"] in existing_keys)
                    or (book["isbn"] and book["isbn"] in existing_isbns)
                    or title_key in existing_titles
                ):
                    skipped.append(book["title"])
                    continue
                timestamp = now_iso()
                columns = list(book.keys()) + ["created_at", "updated_at"]
                values = list(book.values()) + [timestamp, timestamp]
                placeholders = ", ".join("?" for _ in values)
                cursor = connection.execute(
                    f"INSERT INTO books ({', '.join(columns)}) VALUES ({placeholders})", values
                )
                row = connection.execute("SELECT * FROM books WHERE id = ?", (cursor.lastrowid,)).fetchone()
                created_book = row_to_dict(row)
                created.append(created_book)
                if book["open_library_key"]:
                    existing_keys.add(book["open_library_key"])
                if book["isbn"]:
                    existing_isbns.add(book["isbn"])
                existing_titles.add(title_key)

        self.send_json({"created": created, "created_count": len(created), "skipped": skipped, "errors": errors})

    def import_books(self) -> None:
        try:
            payload = self.read_json()
        except ValueError as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        raw_books = payload.get("books")
        mode = str(payload.get("mode") or "merge").strip().lower()
        if mode not in {"merge", "replace"}:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "Import mode must be merge or replace.")
        if not isinstance(raw_books, list) or not raw_books or len(raw_books) > 5000:
            return self.send_error_json(
                HTTPStatus.BAD_REQUEST,
                "Backup must contain between 1 and 5,000 books.",
            )

        validated: list[tuple[dict[str, Any], dict[str, Any]]] = []
        errors: list[str] = []
        backup_keys: set[str] = set()
        backup_isbns: set[str] = set()
        backup_titles: set[tuple[str, str]] = set()
        for index, item in enumerate(raw_books, start=1):
            if not isinstance(item, dict):
                errors.append(f"Book {index} is not an object.")
                continue
            try:
                book = normalize_book(item)
            except ValueError as exc:
                errors.append(f"Book {index}: {exc}")
                continue
            title_key = (book["title"].casefold(), book["authors"].casefold())
            is_duplicate = (
                (book["open_library_key"] and book["open_library_key"] in backup_keys)
                or (book["isbn"] and book["isbn"] in backup_isbns)
                or title_key in backup_titles
            )
            if is_duplicate:
                errors.append(f"Book {index}: duplicate entry for {book['title']}.")
                continue
            if book["open_library_key"]:
                backup_keys.add(book["open_library_key"])
            if book["isbn"]:
                backup_isbns.add(book["isbn"])
            backup_titles.add(title_key)
            validated.append((book, item))
        if errors:
            return self.send_json(
                {"error": "Backup validation failed.", "details": errors[:20]},
                HTTPStatus.BAD_REQUEST,
            )

        def safe_timestamp(value: Any, fallback: str) -> str:
            candidate = str(value or "").strip()[:40]
            if not candidate:
                return fallback
            try:
                datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            except ValueError:
                return fallback
            return candidate

        timestamp = now_iso()
        created_count = 0
        skipped_count = 0
        replaced_count = 0
        with db_connection() as connection:
            existing_rows = connection.execute(
                "SELECT title, authors, isbn, open_library_key FROM books"
            ).fetchall()
            replaced_count = len(existing_rows) if mode == "replace" else 0
            existing_keys = {row["open_library_key"] for row in existing_rows if row["open_library_key"]}
            existing_isbns = {row["isbn"] for row in existing_rows if row["isbn"]}
            existing_titles = {
                (row["title"].strip().casefold(), row["authors"].strip().casefold())
                for row in existing_rows
            }
            if mode == "replace":
                connection.execute("DELETE FROM books")
                existing_keys.clear()
                existing_isbns.clear()
                existing_titles.clear()

            for book, source in validated:
                title_key = (book["title"].casefold(), book["authors"].casefold())
                if (
                    (book["open_library_key"] and book["open_library_key"] in existing_keys)
                    or (book["isbn"] and book["isbn"] in existing_isbns)
                    or title_key in existing_titles
                ):
                    skipped_count += 1
                    continue
                created_at = safe_timestamp(source.get("created_at"), timestamp)
                updated_at = safe_timestamp(source.get("updated_at"), created_at)
                columns = list(book.keys()) + ["created_at", "updated_at"]
                values = list(book.values()) + [created_at, updated_at]
                placeholders = ", ".join("?" for _ in values)
                connection.execute(
                    f"INSERT INTO books ({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
                created_count += 1
                if book["open_library_key"]:
                    existing_keys.add(book["open_library_key"])
                if book["isbn"]:
                    existing_isbns.add(book["isbn"])
                existing_titles.add(title_key)

        self.send_json(
            {
                "mode": mode,
                "created_count": created_count,
                "skipped_count": skipped_count,
                "replaced_count": replaced_count,
                "total_count": created_count + (skipped_count if mode == "merge" else 0),
            }
        )

    def export_books(self) -> None:
        with db_connection() as connection:
            rows = connection.execute("SELECT * FROM books ORDER BY title COLLATE NOCASE").fetchall()
        payload = {
            "app": "Home Library V1",
            "exported_at": now_iso(),
            "books": [row_to_dict(row) for row in rows],
        }
        self.send_json(
            payload,
            headers={"Content-Disposition": f'attachment; filename="home-library-{date.today().isoformat()}.json"'},
        )

    def proxy_cover(self, query: dict[str, list[str]]) -> None:
        source = (query.get("url", [""])[0] or "").strip()
        parsed = urllib.parse.urlparse(source)
        if parsed.scheme != "https" or not allowed_cover_host(parsed.hostname):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid cover source.")
        request = urllib.request.Request(
            source,
            headers={
                "User-Agent": "HomeLibraryV1/1.4",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                final_url = urllib.parse.urlparse(response.geturl())
                if final_url.scheme != "https" or not allowed_cover_host(final_url.hostname):
                    raise ValueError("Cover redirected to an unapproved host.")
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    raise ValueError("Cover response was not an image.")
                content = response.read(6_000_001)
                if len(content) > 6_000_000:
                    raise ValueError("Cover image was too large.")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"Cover proxy failed: {exc}")
            return self.send_error_json(HTTPStatus.BAD_GATEWAY, "Cover image could not be loaded.")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        requested = (STATIC_DIR / relative).resolve()
        try:
            requested.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self.send_error(HTTPStatus.FORBIDDEN)
        if not requested.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        content = requested.read_bytes()
        content_type, _ = mimetypes.guess_type(requested.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        # Always revalidate the small app shell so container updates appear immediately.
        # The service worker still keeps a copy for temporary offline rendering.
        self.send_header("Cache-Control", "no-cache")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob: https://covers.openlibrary.org "
            "https://archive.org https://*.archive.org https://aethonbooks.com; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
            "media-src 'self' blob:",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), LibraryHandler)
    print(f"Home Library V1 listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
