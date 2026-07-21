const state = {
  books: [],
  filter: "all",
  ownership: "all",
  query: "",
  sort: "updated",
  lookupResults: [],
  lookupSelectionToken: 0,
  seriesCandidates: [],
  seriesImportSeries: "",
  seriesGroups: new Map(),
  openSeriesKey: "",
  scanner: null,
  restorePayload: null,
  metadataApply: null,
  firstLoad: true,
};

const elements = {
  grid: document.querySelector("#book-grid"),
  empty: document.querySelector("#empty-state"),
  emptyTitle: document.querySelector("#empty-title"),
  emptyCopy: document.querySelector("#empty-copy"),
  search: document.querySelector("#library-search"),
  sort: document.querySelector("#library-sort"),
  ownership: document.querySelector("#library-ownership"),
  dialog: document.querySelector("#book-dialog"),
  form: document.querySelector("#book-form"),
  dialogTitle: document.querySelector("#dialog-title"),
  dialogKicker: document.querySelector("#dialog-kicker"),
  deleteButton: document.querySelector("#delete-book"),
  refreshBookButton: document.querySelector("#refresh-book-metadata"),
  saveButton: document.querySelector("#save-book"),
  lookupQuery: document.querySelector("#lookup-query"),
  lookupButton: document.querySelector("#lookup-button"),
  lookupStatus: document.querySelector("#lookup-status"),
  lookupResults: document.querySelector("#lookup-results"),
  seriesImportPanel: document.querySelector("#series-import-panel"),
  seriesImportTitle: document.querySelector("#series-import-title"),
  seriesImportSummary: document.querySelector("#series-import-summary"),
  seriesImportBooks: document.querySelector("#series-import-books"),
  seriesImportToggle: document.querySelector("#add-full-series"),
  seriesImportToggleWrap: document.querySelector("#series-import-toggle-wrap"),
  coverUrl: document.querySelector("#book-cover-url"),
  coverImage: document.querySelector("#cover-preview-image"),
  coverPlaceholder: document.querySelector("#cover-preview .cover-placeholder"),
  coverFile: document.querySelector("#book-cover-file"),
  uploadCoverButton: document.querySelector("#upload-cover"),
  progressField: document.querySelector("#progress-field"),
  progress: document.querySelector("#book-current-page"),
  progressOutput: document.querySelector("#progress-output"),
  totalPages: document.querySelector("#book-total-pages"),
  rating: document.querySelector("#book-rating"),
  seriesDialog: document.querySelector("#series-dialog"),
  seriesTitle: document.querySelector("#series-dialog-title"),
  seriesList: document.querySelector("#series-book-list"),
  refreshSeriesButton: document.querySelector("#refresh-series-metadata"),
  restoreButton: document.querySelector("#restore-backup"),
  restoreFile: document.querySelector("#restore-file"),
  restoreDialog: document.querySelector("#restore-dialog"),
  restoreSummary: document.querySelector("#restore-summary"),
  applyRestore: document.querySelector("#apply-restore"),
  scannerDialog: document.querySelector("#scanner-dialog"),
  scannerStatus: document.querySelector("#scanner-status"),
  metadataDialog: document.querySelector("#metadata-dialog"),
  metadataTitle: document.querySelector("#metadata-dialog-title"),
  metadataSummary: document.querySelector("#metadata-summary"),
  metadataChanges: document.querySelector("#metadata-changes"),
  applyMetadata: document.querySelector("#apply-metadata"),
  toast: document.querySelector("#toast"),
};

const statusLabels = {
  tbr: "TBR",
  in_progress: "In progress",
  finished: "Finished",
  dnf: "DNF",
};

const formatLabels = {
  physical: "Physical",
  ebook: "eBook",
  audiobook: "Audiobook",
};

const ownershipLabels = {
  owned: "Owned",
  ku: "KU",
  need_to_purchase: "Need to purchase",
};

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeImageUrl(value = "") {
  if (!value) return "";
  if (/^data:image\/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) && value.length <= 1_400_000) {
    return value;
  }
  try {
    const url = new URL(value, window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? escapeHtml(url.href) : "";
  } catch {
    return "";
  }
}

function displayImageUrl(value = "") {
  if (!value) return "";
  try {
    const url = new URL(value, window.location.origin);
    if (
      url.hostname === "covers.openlibrary.org"
      || url.hostname === "aethonbooks.com"
      || url.hostname === "i.gr-assets.com"
      || url.hostname === "m.media-amazon.com"
      || url.hostname.endsWith(".smushcdn.com")
    ) {
      return `/api/cover?url=${encodeURIComponent(url.href)}`;
    }
  } catch {
    return "";
  }
  return safeImageUrl(value);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json", ...(options.headers || {}) } : options.headers,
  });
  let data;
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function showToast(message, type = "success") {
  elements.toast.textContent = message;
  elements.toast.className = `toast show ${type === "error" ? "error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    elements.toast.className = "toast";
  }, 3200);
}

function setLoading() {
  elements.empty.hidden = true;
  elements.grid.innerHTML = `<div class="loading-grid">${Array.from({ length: 5 }, () => '<div class="skeleton"></div>').join("")}</div>`;
}

async function loadLibrary() {
  if (state.firstLoad) setLoading();
  const params = new URLSearchParams({ status: state.filter, sort: state.sort });
  if (state.query) params.set("q", state.query);
  if (state.ownership !== "all") params.set("ownership", state.ownership);
  try {
    const [library, stats] = await Promise.all([
      api(`/api/books?${params}`),
      api("/api/stats"),
    ]);
    state.books = library.books;
    renderBooks();
    renderStats(stats);
    state.firstLoad = false;
  } catch (error) {
    elements.grid.innerHTML = "";
    elements.empty.hidden = false;
    elements.emptyTitle.textContent = "We couldn’t reach your library";
    elements.emptyCopy.textContent = error.message;
    showToast(error.message, "error");
  }
}

function renderStats(stats) {
  document.querySelector("#stat-total").textContent = stats.total;
  document.querySelector("#stat-reading").textContent = stats.in_progress;
  document.querySelector("#stat-tbr").textContent = stats.tbr;
  document.querySelector("#stat-finished").textContent = stats.finished;
  document.querySelector("#brand-count").textContent = stats.total
    ? `${stats.total} ${stats.total === 1 ? "book" : "books"} on your shelves`
    : "A place for every story";
  document.querySelector("#hero-add").textContent = stats.total ? "Add another book" : "Add your first book";
}

function renderBooks() {
  const candidates = new Map();
  state.books.forEach((book) => {
    const key = seriesKey(book.series);
    if (!key) return;
    if (!candidates.has(key)) candidates.set(key, { name: book.series.trim(), books: [] });
    candidates.get(key).books.push(book);
  });
  state.seriesGroups = new Map([...candidates].filter(([, group]) => group.books.length >= 2));

  const renderedSeries = new Set();
  const cards = [];
  state.books.forEach((book) => {
    const key = seriesKey(book.series);
    const group = state.seriesGroups.get(key);
    if (!group) {
      cards.push(bookCard(book));
      return;
    }
    if (!renderedSeries.has(key)) {
      renderedSeries.add(key);
      cards.push(seriesFolderCard(key, group));
    }
  });
  elements.grid.innerHTML = cards.join("");
  elements.empty.hidden = state.books.length > 0;

  if (!state.books.length) {
    if (state.query) {
      elements.emptyTitle.textContent = "No matching books";
      elements.emptyCopy.textContent = `Nothing on your shelves matches “${state.query}”.`;
    } else if (state.filter !== "all") {
      elements.emptyTitle.textContent = `No ${statusLabels[state.filter].toLowerCase()} books yet`;
      elements.emptyCopy.textContent = "Choose another shelf or update a book’s reading status.";
    } else {
      elements.emptyTitle.textContent = "Your shelves are waiting";
      elements.emptyCopy.textContent = "Add a book and we’ll fetch its cover for you.";
    }
  }

  installCoverFallbacks(elements.grid);
}

function seriesKey(value = "") {
  return String(value).trim().toLocaleLowerCase();
}

function seriesSort(a, b) {
  const aNumber = Number.parseFloat(a.volume);
  const bNumber = Number.parseFloat(b.volume);
  const aOrder = Number.isFinite(aNumber) ? aNumber : Number.POSITIVE_INFINITY;
  const bOrder = Number.isFinite(bNumber) ? bNumber : Number.POSITIVE_INFINITY;
  return aOrder - bOrder || a.title.localeCompare(b.title, undefined, { sensitivity: "base" });
}

function installCoverFallbacks(container) {
  container.querySelectorAll("img[data-cover], img[data-folder-cover], img[data-series-cover]").forEach((image) => {
    image.addEventListener("error", () => {
      image.hidden = true;
      const placeholder = image.nextElementSibling;
      if (placeholder) placeholder.hidden = false;
    }, { once: true });
  });
}

function folderArt(books, context = "folder") {
  const sorted = [...books].sort(seriesSort).slice(0, 4);
  return `
    <span class="folder-preview" data-count="${sorted.length}">
      ${sorted.map((book) => {
        const cover = displayImageUrl(book.cover_url);
        return `<span class="folder-cover">
          ${cover ? `<img data-folder-cover src="${cover}" alt="" loading="lazy">` : ""}
          <span class="folder-cover-fallback" ${cover ? "hidden" : ""}>${escapeHtml(book.volume || book.title.slice(0, 1))}</span>
        </span>`;
      }).join("")}
      ${context === "folder" ? '<span class="folder-gloss"></span>' : ""}
    </span>`;
}

function seriesFolderCard(key, group) {
  const books = [...group.books].sort(seriesSort);
  const finished = books.filter((book) => book.status === "finished").length;
  return `
    <article class="book-card series-card">
      <button class="series-folder-button" type="button" data-series-key="${escapeHtml(key)}" aria-label="Open ${escapeHtml(group.name)} series, ${books.length} books">
        ${folderArt(books)}
        <span class="series-folder-badge">Series</span>
      </button>
      <div class="book-info">
        <h3 class="book-title">${escapeHtml(group.name)}</h3>
        <p class="book-author">${books.length} books in this folder</p>
        <p class="series-label">${finished ? `${finished} finished · ` : ""}Tap to open</p>
      </div>
    </article>`;
}

function bookCard(book) {
  const cover = displayImageUrl(book.cover_url);
  const series = book.series
    ? `${escapeHtml(book.series)}${book.volume ? ` · Book ${escapeHtml(book.volume)}` : ""}`
    : "";
  const rating = book.rating > 0
    ? `<span class="rating-stars" aria-label="${book.rating} out of 5 stars">${"★".repeat(book.rating)}${"☆".repeat(5 - book.rating)}</span>`
    : "<span></span>";
  const pages = book.total_pages > 0
    ? `<span class="page-count">${Number(book.total_pages).toLocaleString()} pages</span>`
    : "";
  const formats = formatSummary(book.formats);
  const ownership = ownershipLabels[book.ownership]
    ? `<span class="ownership-chip ${book.ownership}">${ownershipLabels[book.ownership]}</span>`
    : "";
  const quickAction = `<label class="quick-status"><span class="sr-only">Change reading status</span><select data-quick-status aria-label="Change status for ${escapeHtml(book.title)}">
    <option value="">Set status…</option>
    ${["in_progress", "finished", "dnf"].filter((value) => value !== book.status).map((value) => `<option value="${value}">${statusLabels[value]}</option>`).join("")}
  </select></label>`;
  const progress = book.status === "in_progress"
    ? `<div class="progress-overlay" title="${book.total_pages ? `Page ${book.current_page} of ${book.total_pages}` : "Reading"}"><span style="width:${book.progress}%"></span></div>`
    : "";

  return `
    <article class="book-card" data-book-id="${book.id}">
      <div class="cover-wrap">
        <span class="status-badge ${book.status}">${statusLabels[book.status]}</span>
        <button class="book-cover-button" type="button" data-edit-id="${book.id}" aria-label="Edit ${escapeHtml(book.title)}">
          ${cover ? `<img class="book-cover" data-cover src="${cover}" alt="Cover of ${escapeHtml(book.title)}" loading="lazy">` : ""}
          <span class="book-cover-placeholder" ${cover ? "hidden" : ""}><strong>${escapeHtml(book.title)}</strong></span>
        </button>
        ${progress}
      </div>
      <div class="book-info">
        <div class="book-title-row">
          <h3 class="book-title">${escapeHtml(book.title)}</h3>
          <button class="edit-book" type="button" data-edit-id="${book.id}" aria-label="Edit ${escapeHtml(book.title)}">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 16-.8 4.8L8 20l11-11-4-4L4 16Z"/><path d="m13.5 6.5 4 4"/></svg>
          </button>
        </div>
        <p class="book-author">${escapeHtml(book.authors || "Unknown author")}</p>
        ${series ? `<p class="series-label">${series}</p>` : ""}
        <div class="book-labels">${formats}${ownership}</div>
        <div class="book-meta"><span class="book-meta-info">${rating}${pages}</span>${quickAction}</div>
      </div>
    </article>`;
}

function formatSummary(formats) {
  const values = Array.isArray(formats) ? formats.filter((value) => formatLabels[value]) : [];
  if (!values.length) return "";
  return `<p class="format-summary">${values.map((value) => `<span>${formatLabels[value]}</span>`).join("")}</p>`;
}

function openSeriesDialog(key) {
  const group = state.seriesGroups.get(key);
  if (!group) return;
  state.openSeriesKey = key;
  const books = [...group.books].sort(seriesSort);
  elements.seriesTitle.textContent = group.name;
  elements.seriesList.innerHTML = books.map((book) => {
    const cover = displayImageUrl(book.cover_url);
    const details = [
      book.volume ? `Book ${escapeHtml(book.volume)}` : "",
      book.total_pages ? `${Number(book.total_pages).toLocaleString()} pages` : "",
      ...(Array.isArray(book.formats) ? book.formats.filter((value) => formatLabels[value]).map((value) => formatLabels[value]) : []),
      ownershipLabels[book.ownership] || "",
    ].filter(Boolean).join(" · ");
    return `
      <button class="series-book-row" type="button" data-series-edit="${book.id}" aria-label="Edit ${escapeHtml(book.title)}">
        <span class="series-row-cover">
          ${cover ? `<img data-series-cover src="${cover}" alt="" loading="lazy">` : ""}
          <span ${cover ? "hidden" : ""}>${escapeHtml(book.title.slice(0, 1))}</span>
        </span>
        <span class="series-row-copy">
          <strong>${escapeHtml(book.title)}</strong>
          <small>${escapeHtml(book.authors || "Unknown author")}</small>
          ${details ? `<small class="series-row-details">${details}</small>` : ""}
        </span>
        <span class="status-badge ${book.status}">${statusLabels[book.status]}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>
      </button>`;
  }).join("");
  installCoverFallbacks(elements.seriesDialog);
  elements.seriesDialog.showModal();
}

function closeSeriesDialog() {
  state.openSeriesKey = "";
  elements.seriesDialog.close();
}

function setFilter(filter) {
  state.filter = filter;
  document.querySelectorAll(".filter-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.filter === filter);
  });
  document.querySelector("#collection-title").scrollIntoView({ behavior: "smooth", block: "start" });
  loadLibrary();
}

function resetForm() {
  state.lookupSelectionToken += 1;
  elements.form.reset();
  document.querySelector("#book-id").value = "";
  elements.coverUrl.value = "";
  elements.coverFile.value = "";
  document.querySelector("#book-open-library-key").value = "";
  elements.lookupQuery.value = "";
  elements.lookupResults.innerHTML = "";
  elements.lookupStatus.textContent = "";
  elements.lookupStatus.className = "lookup-status";
  state.lookupResults = [];
  clearSeriesImport();
  setRating(0);
  updateCoverPreview("");
  updateStatusControls();
  elements.deleteButton.hidden = true;
  elements.refreshBookButton.hidden = true;
}

function clearSeriesImport() {
  state.seriesCandidates = [];
  state.seriesImportSeries = "";
  elements.seriesImportPanel.hidden = true;
  elements.seriesImportPanel.classList.remove("loading", "complete");
  elements.seriesImportTitle.textContent = "Add the rest of this series?";
  elements.seriesImportSummary.textContent = "";
  elements.seriesImportBooks.innerHTML = "";
  elements.seriesImportToggle.checked = true;
  elements.seriesImportToggleWrap.hidden = false;
}

function showSeriesImportLoading(series) {
  state.seriesCandidates = [];
  state.seriesImportSeries = series;
  elements.seriesImportPanel.hidden = false;
  elements.seriesImportPanel.classList.add("loading");
  elements.seriesImportPanel.classList.remove("complete");
  elements.seriesImportTitle.textContent = `Finding every ${series} book…`;
  elements.seriesImportSummary.textContent = "Checking Goodreads and your shelves for missing volumes.";
  elements.seriesImportBooks.innerHTML = '<span class="series-import-loader"></span>';
  elements.seriesImportToggleWrap.hidden = true;
}

function renderSeriesImport(data) {
  const missing = Array.isArray(data.missing) ? data.missing : [];
  state.seriesCandidates = missing;
  state.seriesImportSeries = data.series || state.seriesImportSeries;
  elements.seriesImportPanel.classList.remove("loading");
  elements.seriesImportPanel.classList.toggle("complete", missing.length === 0);
  elements.seriesImportToggleWrap.hidden = missing.length === 0;

  if (!missing.length) {
    elements.seriesImportTitle.textContent = `${data.series} is already complete`;
    elements.seriesImportSummary.textContent = data.discovered_count
      ? `All ${data.discovered_count} discovered books are already on your shelves.`
      : "No other numbered books were found for this series.";
    elements.seriesImportBooks.innerHTML = '<span class="series-complete-mark" aria-hidden="true">✓</span>';
    return;
  }

  elements.seriesImportTitle.textContent = `Add ${missing.length} more from ${data.series}?`;
  const existingCopy = data.existing_count
    ? ` ${data.existing_count} ${data.existing_count === 1 ? "book is" : "books are"} already on your shelves.`
    : "";
  elements.seriesImportSummary.textContent = `Missing books will be added as TBR.${existingCopy}`;
  elements.seriesImportBooks.innerHTML = missing.map((book) => {
    const cover = displayImageUrl(book.cover_url);
    const label = book.volume ? `Book ${escapeHtml(book.volume)}` : "Series book";
    return `
      <span class="series-import-book" title="${escapeHtml(book.title)}">
        <span class="series-import-cover">
          ${cover ? `<img src="${cover}" alt="" loading="lazy">` : `<b>${escapeHtml(book.title.slice(0, 1))}</b>`}
        </span>
        <small>${label}</small>
      </span>`;
  }).join("");
}

async function discoverSeries(
  series,
  authors,
  openLibraryKey,
  selectedTitle,
  selectionToken,
  seriesUrl = "",
  selectedVolume = "",
) {
  if (!series) return clearSeriesImport();
  showSeriesImportLoading(series);
  const params = new URLSearchParams({ series });
  if (authors) params.set("author", authors);
  if (openLibraryKey) params.set("exclude_key", openLibraryKey);
  if (selectedTitle) params.set("exclude_title", selectedTitle);
  if (seriesUrl) params.set("series_url", seriesUrl);
  if (selectedVolume) params.set("exclude_volume", selectedVolume);
  try {
    const data = await api(`/api/lookup/series?${params}`);
    if (selectionToken !== state.lookupSelectionToken) return;
    renderSeriesImport(data);
  } catch {
    if (selectionToken !== state.lookupSelectionToken) return;
    clearSeriesImport();
  }
}

function openAddDialog() {
  resetForm();
  elements.dialogTitle.textContent = "Add a book";
  elements.dialogKicker.textContent = "Add to your shelves";
  elements.saveButton.textContent = "Save book";
  elements.dialog.showModal();
  setTimeout(() => elements.lookupQuery.focus(), 80);
}

function openEditDialog(bookId) {
  const book = state.books.find((item) => item.id === Number(bookId));
  if (!book) return;
  resetForm();
  elements.dialogTitle.textContent = "Edit book";
  elements.dialogKicker.textContent = "Update your copy";
  elements.saveButton.textContent = "Save changes";
  elements.deleteButton.hidden = false;
  elements.refreshBookButton.hidden = false;

  for (const [key, value] of Object.entries(book)) {
    if (key === "formats") {
      setFormats(value);
      continue;
    }
    const input = elements.form.elements.namedItem(key);
    if (!input) continue;
    if (input instanceof RadioNodeList) {
      input.value = value;
    } else {
      input.value = value ?? "";
    }
  }
  elements.totalPages.value = book.total_pages || "";
  elements.progress.max = String(Math.max(Number(book.total_pages) || 0, 1));
  elements.progress.value = String(book.current_page || 0);
  setRating(book.rating || 0);
  updateCoverPreview(book.cover_url);
  updateStatusControls();
  elements.dialog.showModal();
}

function closeDialog() {
  elements.dialog.close();
}

function setFormats(formats) {
  const selected = new Set(Array.isArray(formats) ? formats : []);
  document.querySelectorAll('input[name="formats"]').forEach((checkbox) => {
    checkbox.checked = selected.has(checkbox.value);
  });
}

function updateCoverPreview(url) {
  elements.coverUrl.value = url || "";
  const safe = displayImageUrl(url);
  if (safe) {
    elements.coverImage.src = safe;
    elements.coverImage.hidden = false;
    elements.coverPlaceholder.hidden = true;
  } else {
    elements.coverImage.removeAttribute("src");
    elements.coverImage.hidden = true;
    elements.coverPlaceholder.hidden = false;
  }
}

async function uploadCoverImage(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  if (!new Set(["image/jpeg", "image/png", "image/webp"]).has(file.type)) {
    return showToast("Choose a JPG, PNG, or WebP image.", "error");
  }
  if (file.size > 12_000_000) return showToast("Choose an image smaller than 12 MB.", "error");

  elements.uploadCoverButton.disabled = true;
  elements.uploadCoverButton.textContent = "Preparing…";
  const objectUrl = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.decoding = "async";
    image.src = objectUrl;
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("That image could not be opened."));
    });
    const initialScale = Math.min(1, 900 / image.naturalWidth, 1350 / image.naturalHeight);
    let encoded = "";
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const scale = initialScale * Math.pow(0.82, attempt);
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      const context = canvas.getContext("2d");
      context.imageSmoothingEnabled = true;
      context.imageSmoothingQuality = "high";
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const quality = Math.max(0.62, 0.84 - attempt * 0.04);
      encoded = canvas.toDataURL("image/webp", quality);
      if (!encoded.startsWith("data:image/webp")) encoded = canvas.toDataURL("image/jpeg", quality);
      if (encoded.length <= 1_300_000) break;
    }
    if (!encoded || encoded.length > 1_300_000) throw new Error("That cover could not be compressed below 1 MB.");
    updateCoverPreview(encoded);
    showToast("Cover uploaded. Save the book to keep it.");
  } catch (error) {
    showToast(error.message || "That cover could not be prepared.", "error");
  } finally {
    URL.revokeObjectURL(objectUrl);
    elements.uploadCoverButton.disabled = false;
    elements.uploadCoverButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4m0 0L8 8m4-4 4 4M5 20h14"/></svg>Upload cover';
  }
}

elements.coverImage.addEventListener("error", () => {
  elements.coverImage.hidden = true;
  elements.coverPlaceholder.hidden = false;
});

function updateStatusControls() {
  const status = new FormData(elements.form).get("status") || "tbr";
  const totalPages = Math.max(0, Number(elements.totalPages.value) || 0);
  let currentPage = Math.max(0, Number(elements.progress.value) || 0);
  elements.progressField.hidden = status !== "in_progress";
  if (status === "finished") {
    currentPage = totalPages || currentPage;
  } else if (status === "tbr") {
    currentPage = 0;
  }
  currentPage = totalPages ? Math.min(currentPage, totalPages) : 0;
  elements.progress.max = String(Math.max(totalPages, 1));
  elements.progress.value = String(currentPage);
  elements.progress.disabled = totalPages === 0;
  elements.progressOutput.value = totalPages
    ? `Page ${currentPage.toLocaleString()} of ${totalPages.toLocaleString()}`
    : "Add total pages first";
}

function setRating(rating) {
  const value = Number(rating) || 0;
  elements.rating.value = value;
  document.querySelectorAll("#star-rating button[data-rating]").forEach((button) => {
    const buttonValue = Number(button.dataset.rating);
    button.classList.toggle("active", buttonValue > 0 && buttonValue <= value);
  });
}

async function lookupBooks() {
  const query = elements.lookupQuery.value.trim();
  if (query.length < 2) {
    elements.lookupStatus.textContent = "Enter a title, author, or ISBN first.";
    elements.lookupStatus.className = "lookup-status error";
    return;
  }
  elements.lookupButton.disabled = true;
  elements.lookupButton.textContent = "Finding…";
  elements.lookupStatus.textContent = "Searching the book catalog...";
  elements.lookupStatus.className = "lookup-status";
  elements.lookupResults.innerHTML = "";
  try {
    const data = await api(`/api/lookup?q=${encodeURIComponent(query)}`);
    state.lookupResults = data.results;
    elements.lookupStatus.textContent = data.count
      ? `Found ${data.count} ${data.count === 1 ? "match" : "matches"}. Choose the right edition.`
      : "No matches found. You can enter the details below.";
    elements.lookupResults.innerHTML = data.results.map((book, index) => {
      const cover = displayImageUrl(book.cover_url);
      return `
        <button class="lookup-result" type="button" data-result-index="${index}">
          <span class="lookup-cover">${cover ? `<img src="${cover}" alt="" loading="lazy">` : "No cover"}</span>
          <span class="lookup-result-copy">
            <strong>${escapeHtml(book.title)}</strong>
            <small>${escapeHtml(book.authors || "Unknown author")}${book.year ? ` · ${book.year}` : ""}${book.total_pages ? ` · ${Number(book.total_pages).toLocaleString()} pages` : ""}</small>
          </span>
        </button>`;
    }).join("");
  } catch (error) {
    elements.lookupStatus.textContent = error.message;
    elements.lookupStatus.className = "lookup-status error";
  } finally {
    elements.lookupButton.disabled = false;
    elements.lookupButton.textContent = "Search";
  }
}

async function chooseLookupResult(index) {
  const book = state.lookupResults[index];
  if (!book) return;
  const selectionToken = ++state.lookupSelectionToken;
  document.querySelector("#book-title").value = book.title || "";
  document.querySelector("#book-authors").value = book.authors || "";
  document.querySelector("#book-isbn").value = book.isbn || "";
  document.querySelector("#book-series").value = "";
  document.querySelector("#book-volume").value = "";
  document.querySelector("#book-total-pages").value = book.total_pages || "";
  document.querySelector("#book-open-library-key").value = book.open_library_key || "";
  updateCoverPreview(book.cover_url || "");
  clearSeriesImport();
  elements.lookupResults.innerHTML = "";
  updateStatusControls();
  elements.lookupStatus.textContent = `Selected “${book.title}”. Checking edition and series details…`;

  const params = new URLSearchParams();
  if (book.isbn && book.exact_edition) params.set("isbn", book.isbn);
  if (book.open_library_key) params.set("work_key", book.open_library_key);
  if (book.cover_id) params.set("cover_id", book.cover_id);
  let selectedSeries = "";
  let selectedSeriesUrl = "";
  if (params.size) {
    try {
      const details = await api(`/api/lookup/details?${params}`);
      if (selectionToken !== state.lookupSelectionToken) return;
      if (details.title) document.querySelector("#book-title").value = details.title;
      if (details.authors) document.querySelector("#book-authors").value = details.authors;
      if (details.isbn) document.querySelector("#book-isbn").value = details.isbn;
      if (details.total_pages) document.querySelector("#book-total-pages").value = details.total_pages;
      if (details.cover_url) updateCoverPreview(details.cover_url);
      if (details.series) {
        selectedSeries = details.series;
        document.querySelector("#book-series").value = details.series;
      }
      selectedSeriesUrl = details.series_url || "";
      if (details.volume) document.querySelector("#book-volume").value = details.volume;
      if (details.format_hint) setFormats([details.format_hint]);
      updateStatusControls();
    } catch {
      // The initial search result is still usable if edition enrichment is unavailable.
    }
  }
  if (selectedSeries && selectionToken === state.lookupSelectionToken) {
    elements.lookupStatus.textContent = `Selected “${book.title}”. Checking the rest of ${selectedSeries}…`;
    await discoverSeries(
      selectedSeries,
      document.querySelector("#book-authors").value,
      book.open_library_key,
      document.querySelector("#book-title").value,
      selectionToken,
      selectedSeriesUrl,
      document.querySelector("#book-volume").value,
    );
  }
  if (selectionToken !== state.lookupSelectionToken) return;
  elements.lookupStatus.textContent = `Selected “${book.title}”. You can adjust anything below.`;
  document.querySelector("#book-title").focus();
}

async function saveBook(event) {
  event.preventDefault();
  if (!elements.form.reportValidity()) return;
  const formData = new FormData(elements.form);
  const data = Object.fromEntries(formData.entries());
  const bookId = data.id;
  delete data.id;
  data.formats = formData.getAll("formats");
  data.current_page = Number(data.current_page || 0);
  data.rating = Number(data.rating || 0);
  data.total_pages = Number(data.total_pages || 0);
  elements.saveButton.disabled = true;
  elements.saveButton.textContent = "Saving…";
  try {
    await api(bookId ? `/api/books/${bookId}` : "/api/books", {
      method: bookId ? "PUT" : "POST",
      body: JSON.stringify(data),
    });
    let seriesCreated = 0;
    let seriesImportFailed = false;
    const seriesStillMatches = String(data.series || "").trim().toLocaleLowerCase()
      === state.seriesImportSeries.trim().toLocaleLowerCase();
    if (!bookId && elements.seriesImportToggle.checked && state.seriesCandidates.length && seriesStillMatches) {
      try {
        const result = await api("/api/books/series", {
          method: "POST",
          body: JSON.stringify({ books: state.seriesCandidates, formats: data.formats, ownership: data.ownership }),
        });
        seriesCreated = result.created_count || 0;
      } catch {
        seriesImportFailed = true;
      }
    }
    closeDialog();
    if (bookId) {
      showToast("Book updated.");
    } else if (seriesCreated) {
      showToast(`Book added, plus ${seriesCreated} ${seriesCreated === 1 ? "series book" : "series books"} as TBR.`);
    } else if (seriesImportFailed) {
      showToast("Book added, but the rest of the series could not be added.", "error");
    } else {
      showToast("Book added to your library.");
    }
    await loadLibrary();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.saveButton.disabled = false;
    elements.saveButton.textContent = bookId ? "Save changes" : "Save book";
  }
}

async function deleteBook() {
  const bookId = document.querySelector("#book-id").value;
  const title = document.querySelector("#book-title").value;
  if (!bookId || !window.confirm(`Remove “${title}” from your library? This cannot be undone.`)) return;
  elements.deleteButton.disabled = true;
  try {
    await api(`/api/books/${bookId}`, { method: "DELETE" });
    closeDialog();
    showToast("Book removed.");
    await loadLibrary();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.deleteButton.disabled = false;
  }
}

async function quickStatus(bookId, status, control) {
  if (!status) return;
  control.disabled = true;
  try {
    await api(`/api/books/${bookId}`, {
      method: "PUT",
      body: JSON.stringify({ status }),
    });
    const messages = {
      in_progress: "Moved to In Progress.",
      finished: "Marked as Finished. Nice work!",
      dnf: "Marked as DNF.",
    };
    showToast(messages[status] || "Reading status updated.");
    await loadLibrary();
  } catch (error) {
    control.disabled = false;
    control.value = "";
    showToast(error.message, "error");
  }
}

function closeRestoreDialog() {
  state.restorePayload = null;
  if (elements.restoreDialog.open) elements.restoreDialog.close();
}

async function chooseRestoreFile(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  if (file.size > 10_000_000) return showToast("That backup is larger than 10 MB.", "error");
  try {
    const payload = JSON.parse(await file.text());
    if (!payload || !Array.isArray(payload.books) || !payload.books.length) {
      throw new Error("This backup does not contain any books.");
    }
    state.restorePayload = payload;
    elements.restoreSummary.textContent = `${file.name} contains ${payload.books.length.toLocaleString()} ${payload.books.length === 1 ? "book" : "books"}.`;
    document.querySelector('input[name="restore-mode"][value="merge"]').checked = true;
    elements.restoreDialog.showModal();
  } catch (error) {
    showToast(error.message || "That file is not a valid Home Library backup.", "error");
  }
}

async function applyRestore() {
  if (!state.restorePayload) return;
  const mode = document.querySelector('input[name="restore-mode"]:checked')?.value || "merge";
  if (mode === "replace" && !window.confirm("Replace every book currently in this library with the selected backup?")) return;
  elements.applyRestore.disabled = true;
  elements.applyRestore.textContent = "Restoring…";
  try {
    const result = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ books: state.restorePayload.books, mode }),
    });
    closeRestoreDialog();
    await loadLibrary();
    showToast(mode === "replace"
      ? `Library restored with ${result.created_count.toLocaleString()} books.`
      : `${result.created_count.toLocaleString()} added; ${result.skipped_count.toLocaleString()} already here.`);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.applyRestore.disabled = false;
    elements.applyRestore.textContent = "Restore books";
  }
}

async function closeScanner() {
  const scanner = state.scanner;
  state.scanner = null;
  if (scanner) {
    try {
      await scanner.clear();
    } catch {
      // The scanner may already be stopped after a successful file scan.
    }
  }
  document.querySelector("#isbn-reader").innerHTML = "";
  if (elements.scannerDialog.open) elements.scannerDialog.close();
}

async function useScannedIsbn(decodedText) {
  const isbn = String(decodedText || "").toUpperCase().replace(/[^0-9X]/g, "");
  if (!((isbn.length === 13 && /^(978|979)/.test(isbn)) || /^\d{9}[\dX]$/.test(isbn))) {
    elements.scannerStatus.textContent = "That barcode is not an ISBN. Try the 978/979 barcode beside it.";
    return;
  }
  await closeScanner();
  elements.lookupQuery.value = isbn;
  elements.lookupStatus.textContent = `ISBN ${isbn} scanned. Searching…`;
  await lookupBooks();
}

function openScanner() {
  if (typeof Html5QrcodeScanner === "undefined") {
    return showToast("The barcode scanner could not be loaded.", "error");
  }
  elements.scannerStatus.textContent = "Camera access is requested only after you choose a camera.";
  elements.scannerDialog.showModal();
  const formats = typeof Html5QrcodeSupportedFormats === "undefined" ? undefined : [
    Html5QrcodeSupportedFormats.EAN_13,
    Html5QrcodeSupportedFormats.EAN_8,
    Html5QrcodeSupportedFormats.UPC_A,
    Html5QrcodeSupportedFormats.UPC_E,
  ];
  const scanTypes = typeof Html5QrcodeScanType === "undefined" ? undefined : [
    Html5QrcodeScanType.SCAN_TYPE_CAMERA,
    Html5QrcodeScanType.SCAN_TYPE_FILE,
  ];
  state.scanner = new Html5QrcodeScanner("isbn-reader", {
    fps: 10,
    qrbox: { width: 280, height: 130 },
    rememberLastUsedCamera: true,
    ...(scanTypes ? { supportedScanTypes: scanTypes } : {}),
    ...(formats ? { formatsToSupport: formats } : {}),
  }, false);
  state.scanner.render(useScannedIsbn, () => {});
}

function closeMetadataDialog() {
  state.metadataApply = null;
  if (elements.metadataDialog.open) elements.metadataDialog.close();
}

function metadataValue(value) {
  if (value === null || value === undefined || value === "" || value === 0) return "Not set";
  return String(value);
}

function openMetadataReview(title, summary, changes, applySelected) {
  elements.metadataTitle.textContent = title;
  elements.metadataSummary.textContent = summary;
  elements.metadataChanges.innerHTML = changes.map((change, index) => {
    const coverPreview = change.field === "cover_url"
      ? `<span class="metadata-cover-pair">
          ${change.before ? `<img src="${displayImageUrl(change.before)}" alt="Current cover">` : '<i>None</i>'}
          <b aria-hidden="true">→</b>
          ${change.after ? `<img src="${displayImageUrl(change.after)}" alt="New cover">` : '<i>None</i>'}
        </span>`
      : `<small>${escapeHtml(metadataValue(change.before))} <b aria-hidden="true">→</b> ${escapeHtml(metadataValue(change.after))}</small>`;
    return `<label class="metadata-change">
      <input type="checkbox" data-metadata-index="${index}" checked>
      <span><strong>${escapeHtml(change.label)}</strong>${change.detail ? `<small>${escapeHtml(change.detail)}</small>` : coverPreview}</span>
    </label>`;
  }).join("");
  state.metadataApply = async () => {
    const selected = [...elements.metadataChanges.querySelectorAll("input:checked")]
      .map((input) => changes[Number(input.dataset.metadataIndex)])
      .filter(Boolean);
    if (!selected.length) return showToast("Choose at least one change.", "error");
    await applySelected(selected);
  };
  elements.metadataDialog.showModal();
}

async function latestBookMetadata(book) {
  const params = new URLSearchParams();
  if (book.isbn) params.set("isbn", book.isbn);
  if (book.open_library_key) params.set("work_key", book.open_library_key);
  if (params.size) return api(`/api/lookup/details?${params}`);
  const found = await api(`/api/lookup?q=${encodeURIComponent(`${book.title} ${book.authors || ""}`)}`);
  const match = found.results?.[0];
  if (!match) throw new Error("No matching catalog record was found.");
  const detailParams = new URLSearchParams();
  if (match.isbn) detailParams.set("isbn", match.isbn);
  if (match.open_library_key) detailParams.set("work_key", match.open_library_key);
  if (!detailParams.size) return match;
  return { ...match, ...(await api(`/api/lookup/details?${detailParams}`)) };
}

async function refreshBookMetadata() {
  const bookId = Number(document.querySelector("#book-id").value);
  const book = state.books.find((item) => item.id === bookId);
  if (!book) return;
  elements.refreshBookButton.disabled = true;
  elements.refreshBookButton.textContent = "Checking…";
  try {
    const latest = await latestBookMetadata(book);
    const fields = [
      ["cover_url", "Cover photo"],
      ["total_pages", "Page count"],
      ["isbn", "ISBN"],
    ];
    const changes = fields.flatMap(([field, label]) => {
      const before = field === "total_pages" ? Number(book[field] || 0) : String(book[field] || "");
      const after = field === "total_pages" ? Number(latest[field] || 0) : String(latest[field] || "");
      return after && before !== after ? [{ field, label, before, after }] : [];
    });
    if (!changes.length) return showToast("Cover, pages, and ISBN are already up to date.");
    openMetadataReview(
      `Refresh ${book.title}`,
      "Confirm each book-level change you want to keep.",
      changes,
      async (selected) => {
        elements.applyMetadata.disabled = true;
        try {
          const patch = Object.fromEntries(selected.map((change) => [change.field, change.after]));
          await api(`/api/books/${book.id}`, { method: "PUT", body: JSON.stringify(patch) });
          closeMetadataDialog();
          await loadLibrary();
          if (patch.cover_url) updateCoverPreview(patch.cover_url);
          if (patch.total_pages) elements.totalPages.value = patch.total_pages;
          if (patch.isbn) document.querySelector("#book-isbn").value = patch.isbn;
          updateStatusControls();
          showToast(`${selected.length} metadata ${selected.length === 1 ? "change" : "changes"} applied.`);
        } finally {
          elements.applyMetadata.disabled = false;
        }
      },
    );
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.refreshBookButton.disabled = false;
    elements.refreshBookButton.textContent = "Refresh metadata";
  }
}

function sameCatalogBook(book, candidate) {
  if (book.isbn && candidate.isbn && book.isbn === candidate.isbn) return true;
  if (book.open_library_key && candidate.open_library_key && book.open_library_key === candidate.open_library_key) return true;
  return String(book.title).trim().toLocaleLowerCase() === String(candidate.title).trim().toLocaleLowerCase();
}

async function refreshSeriesMetadata() {
  const group = state.seriesGroups.get(state.openSeriesKey);
  if (!group) return;
  const books = [...group.books].sort(seriesSort);
  const source = books.find((book) => book.isbn || book.open_library_key) || books[0];
  elements.refreshSeriesButton.disabled = true;
  elements.refreshSeriesButton.textContent = "Checking…";
  try {
    let seriesUrl = "";
    try {
      seriesUrl = (await latestBookMetadata(source)).series_url || "";
    } catch {
      // Series discovery can still use its catalog fallbacks without a Goodreads URL.
    }
    const params = new URLSearchParams({ series: group.name, author: source.authors || "" });
    if (seriesUrl) params.set("series_url", seriesUrl);
    const catalog = await api(`/api/lookup/series?${params}`);
    const changes = [];
    for (const candidate of catalog.books || []) {
      const existing = books.find((book) => sameCatalogBook(book, candidate));
      if (!existing) {
        changes.push({
          kind: "add",
          label: `Add ${candidate.volume ? `Book ${candidate.volume}: ` : ""}${candidate.title}`,
          detail: "TBR • same ownership and format as the first book in this folder",
          candidate,
        });
        continue;
      }
      const patch = {};
      if (candidate.series && candidate.series !== existing.series) patch.series = candidate.series;
      if (candidate.volume && String(candidate.volume) !== String(existing.volume || "")) patch.volume = candidate.volume;
      if (Object.keys(patch).length) {
        changes.push({
          kind: "update",
          label: `Correct ${existing.title}`,
          detail: Object.entries(patch).map(([key, value]) => `${key === "volume" ? "book number" : "series"}: ${value}`).join("; "),
          book: existing,
          patch,
        });
      }
    }
    if (!changes.length) return showToast(`${group.name} is already up to date.`);
    openMetadataReview(
      `Refresh ${group.name}`,
      "Confirm missing books and series-order corrections individually.",
      changes,
      async (selected) => {
        elements.applyMetadata.disabled = true;
        try {
          const updates = selected.filter((change) => change.kind === "update");
          const additions = selected.filter((change) => change.kind === "add").map((change) => change.candidate);
          for (const change of updates) {
            await api(`/api/books/${change.book.id}`, { method: "PUT", body: JSON.stringify(change.patch) });
          }
          let added = 0;
          if (additions.length) {
            const result = await api("/api/books/series", {
              method: "POST",
              body: JSON.stringify({ books: additions, formats: source.formats, ownership: source.ownership }),
            });
            added = result.created_count || 0;
          }
          closeMetadataDialog();
          closeSeriesDialog();
          await loadLibrary();
          showToast(`${updates.length} corrected; ${added} added as TBR.`);
        } finally {
          elements.applyMetadata.disabled = false;
        }
      },
    );
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.refreshSeriesButton.disabled = false;
    elements.refreshSeriesButton.textContent = "Refresh series";
  }
}

document.querySelectorAll("#header-add, #hero-add, #empty-add").forEach((button) => {
  button.addEventListener("click", openAddDialog);
});
document.querySelectorAll("#close-dialog, #cancel-dialog").forEach((button) => {
  button.addEventListener("click", closeDialog);
});
document.querySelectorAll(".filter-tab").forEach((button) => {
  button.addEventListener("click", () => setFilter(button.dataset.filter));
});
document.querySelectorAll("[data-stat-filter]").forEach((button) => {
  button.addEventListener("click", () => setFilter(button.dataset.statFilter));
});

elements.search.addEventListener("input", () => {
  clearTimeout(elements.search.timer);
  elements.search.timer = setTimeout(() => {
    state.query = elements.search.value.trim();
    loadLibrary();
  }, 220);
});
elements.sort.addEventListener("change", () => {
  state.sort = elements.sort.value;
  loadLibrary();
});
elements.ownership.addEventListener("change", () => {
  state.ownership = elements.ownership.value;
  loadLibrary();
});
elements.grid.addEventListener("click", (event) => {
  const seriesButton = event.target.closest("[data-series-key]");
  if (seriesButton) return openSeriesDialog(seriesButton.dataset.seriesKey);
  const editButton = event.target.closest("[data-edit-id]");
  if (editButton) return openEditDialog(editButton.dataset.editId);
});
elements.grid.addEventListener("change", (event) => {
  const control = event.target.closest("[data-quick-status]");
  if (!control) return;
  const card = control.closest("[data-book-id]");
  quickStatus(card.dataset.bookId, control.value, control);
});
document.querySelector("#close-series-dialog").addEventListener("click", closeSeriesDialog);
elements.refreshSeriesButton.addEventListener("click", refreshSeriesMetadata);
elements.seriesList.addEventListener("click", (event) => {
  const bookButton = event.target.closest("[data-series-edit]");
  if (!bookButton) return;
  closeSeriesDialog();
  openEditDialog(bookButton.dataset.seriesEdit);
});
elements.lookupButton.addEventListener("click", lookupBooks);
elements.lookupQuery.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    lookupBooks();
  }
});
elements.lookupResults.addEventListener("click", (event) => {
  const result = event.target.closest("[data-result-index]");
  if (result) chooseLookupResult(Number(result.dataset.resultIndex));
});
elements.form.addEventListener("submit", saveBook);
elements.deleteButton.addEventListener("click", deleteBook);
elements.refreshBookButton.addEventListener("click", refreshBookMetadata);
elements.uploadCoverButton.addEventListener("click", () => elements.coverFile.click());
elements.coverFile.addEventListener("change", uploadCoverImage);
elements.restoreButton.addEventListener("click", () => elements.restoreFile.click());
elements.restoreFile.addEventListener("change", chooseRestoreFile);
document.querySelectorAll("#close-restore-dialog, #cancel-restore").forEach((button) => button.addEventListener("click", closeRestoreDialog));
elements.applyRestore.addEventListener("click", applyRestore);
document.querySelector("#scan-isbn").addEventListener("click", openScanner);
document.querySelectorAll("#close-scanner-dialog, #cancel-scanner").forEach((button) => button.addEventListener("click", closeScanner));
document.querySelectorAll("#close-metadata-dialog, #cancel-metadata").forEach((button) => button.addEventListener("click", closeMetadataDialog));
elements.applyMetadata.addEventListener("click", async () => {
  if (!state.metadataApply) return;
  try {
    await state.metadataApply();
  } catch (error) {
    showToast(error.message, "error");
    elements.applyMetadata.disabled = false;
  }
});
document.querySelector("#clear-cover").addEventListener("click", () => updateCoverPreview(""));
document.querySelectorAll('input[name="status"]').forEach((radio) => radio.addEventListener("change", updateStatusControls));
elements.progress.addEventListener("input", () => {
  const totalPages = Math.max(0, Number(elements.totalPages.value) || 0);
  const currentPage = Math.max(0, Number(elements.progress.value) || 0);
  elements.progressOutput.value = totalPages
    ? `Page ${currentPage.toLocaleString()} of ${totalPages.toLocaleString()}`
    : "Add total pages first";
});
elements.totalPages.addEventListener("input", updateStatusControls);
document.querySelector("#star-rating").addEventListener("click", (event) => {
  const button = event.target.closest("[data-rating]");
  if (button) setRating(button.dataset.rating);
});
elements.dialog.addEventListener("click", (event) => {
  if (event.target === elements.dialog) closeDialog();
});
elements.seriesDialog.addEventListener("click", (event) => {
  if (event.target === elements.seriesDialog) closeSeriesDialog();
});
elements.restoreDialog.addEventListener("click", (event) => {
  if (event.target === elements.restoreDialog) closeRestoreDialog();
});
elements.restoreDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeRestoreDialog();
});
elements.scannerDialog.addEventListener("click", (event) => {
  if (event.target === elements.scannerDialog) closeScanner();
});
elements.scannerDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeScanner();
});
elements.metadataDialog.addEventListener("click", (event) => {
  if (event.target === elements.metadataDialog) closeMetadataDialog();
});
elements.metadataDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeMetadataDialog();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !elements.dialog.open && !elements.seriesDialog.open && !elements.restoreDialog.open && !elements.scannerDialog.open && !elements.metadataDialog.open && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
    event.preventDefault();
    elements.search.focus();
  }
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/service-worker.js").catch(() => {}));
}

loadLibrary();
