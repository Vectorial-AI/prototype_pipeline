# `/upload-html` — Folder Deploy Upgrade

## What Changed

The `/upload-html` endpoint previously only accepted a **single `.html` file**. It now accepts a **zip archive of a complete site folder** (containing at least one HTML file plus any linked assets), deployable via a Vercel Blob URL or a direct file upload.

---

## Before

| Input | Accepted |
|---|---|
| Single `.html` file (direct upload) | ✅ |
| Vercel Blob URL → single `.html` | ✅ |
| Zip of a folder | ❌ |

The downloaded content was always written as a single `index.html` and deployed alone — no assets, no subfolders.

---

## After

| Input | Accepted |
|---|---|
| Single `.html` file (direct upload) | ✅ (unchanged) |
| Vercel Blob URL → single `.html` | ✅ (unchanged) |
| `.zip` folder archive (direct upload) | ✅ **new** |
| Vercel Blob URL → `.zip` folder archive | ✅ **new** |

---

## How It Works Now

### Zip detection
- For a **direct file upload**: the filename extension (`.zip` vs `.html`) determines the mode.
- For a **`blob_url`**: the URL path extension is checked first; if ambiguous or missing, the magic bytes of the downloaded content are sniffed using `zipfile.is_zipfile()`. This works even when Vercel Blob URLs contain query params or lack a clear extension.

### Zip extraction
1. The zip is extracted into a temporary directory.
2. **Zip-slip protection**: every entry path is checked against the real output directory — any entry that would escape the directory is rejected with a `400`.
3. **Single top-level folder unwrap**: if the zip wraps everything inside one subfolder (e.g. `my-site/index.html`, `my-site/styles.css`), that folder is automatically unwrapped so files land at the root. Netlify requires `index.html` to be at the root to serve it correctly.
4. At least one `.html` file must exist anywhere inside the zip — otherwise the request is rejected with a `400`.

### Deployment
The entire extracted directory (all HTML, CSS, JS, images, fonts, etc.) is zipped and sent to Netlify as a single deploy — identical to how the `/process` endpoint deploys generated prototypes.

---

## New Error Cases

| HTTP Status | Reason |
|---|---|
| `400` | Zip contains no `.html` file |
| `400` | Zip contains unsafe/traversal paths |
| `400` | File is not a valid zip archive |
| `400` | Uploaded file extension is not `.zip` or `.html` |

---

## Code Changes

**`api/index.py`**

- Added `shutil` to top-level imports.
- Added `_extract_zip_to_dir(contents, output_dir)` — handles extraction, zip-slip check, and single-folder unwrap.
- Added `_find_html_files(directory)` — walks the extracted tree and returns all `.html` paths.
- Updated `upload_html` endpoint — branching logic for zip vs HTML mode; zip path calls the two helpers above before deploying.
