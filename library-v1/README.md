# Home Library V1

A private, Dockerized home book catalog. Track books as **TBR**, **In Progress**, **Finished**, or **DNF**, record reading progress and ratings, and use [Goodreads](https://www.goodreads.com/) as the primary source for book and series metadata.

## Start it

From this directory:

```powershell
docker-compose up -d --build
```

Open <http://localhost:8787>.

Your catalog is stored in `./data/library.db`. Rebuilding or updating the container does not remove it.

## Stop it

```powershell
docker-compose down
```

## Backups

Use the download icon in the app header to save a human-readable JSON backup. The restore icon can safely merge a backup or, after an extra confirmation, replace the current library. For a complete server-side backup, copy `data/library.db` while the app is stopped.

## V1 features

- Search by title, author, or ISBN, then resolve the selected edition through Goodreads
- Automatic Goodreads, Open Library, and official-publisher cover art through a cached local image proxy
- Cover chooser when catalog sources provide different artwork, plus post-add cover management for selecting, uploading, or removing artwork
- Manual JPG, PNG, or WebP cover uploads, resized locally and included in database and JSON backups
- TBR, In Progress, Finished, and DNF shelves, editable from each book
- Matching-edition ISBNs and page counts instead of arbitrary work-level ISBNs
- Goodreads-primary titles, authors, ISBNs, page counts, series names, and book numbers
- Open Library and official Aethon catalog fallbacks when Goodreads omits a field or is unavailable
- Optional full-series discovery: add every missing volume as TBR and Need to Purchase using the selected book's formats
- Page-based reading progress from page 0 through the book's total pages
- Multi-select Physical, eBook, and Audiobook formats
- Owned, Kindle Unlimited, and Need to Purchase ownership states and filtering
- 1–5 star rating, series, volume, and notes
- Automatic series folders with multi-cover artwork and ordered drill-in
- Search, filtering, sorting, and overview counts
- Mobile-first browsing and full-screen phone editing, plus an installable PWA shell
- Phone-camera or image-file ISBN barcode scanning using the locally bundled `html5-qrcode` library
- Confirm-before-apply metadata refresh for a book's cover, page count, and ISBN, plus series-folder discovery and order corrections
- Local SQLite persistence plus JSON export, safe-merge restore, and confirmed full replacement
- `/healthz` container health endpoint

No account or API key is required. Goodreads discontinued its public API, so this personal app reads structured metadata from its public book and series pages. If Goodreads is unavailable or changes those pages, Open Library, official publisher metadata, and manual entry remain available.

The bundled barcode scanner is `html5-qrcode` 2.3.8. Its upstream license is preserved in `static/vendor/html5-qrcode.LICENSE.txt`.

## Configuration

The host port defaults to `8787`. Change the left side of the port mapping in `docker-compose.yml` if needed:

```yaml
ports:
  - "8787:8080"
```
