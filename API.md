# Prototype Pipeline API

**Base URL:** `https://prototype-pipeline.vercel.app`

All responses are JSON. CORS is open (`*`).

---

## Endpoints

### 1. `GET /health`

Check that the API is up and env vars are configured.

**Request:** none

**Response `200`**
```json
{
  "status": "ok",
  "netlify_token_set": true,
  "figma_token_set": true
}
```

---

### 2. `POST /process`

Full Figma → prototype pipeline. Give it a Figma URL and token — it fetches the file and all frame images directly from the Figma API, converts them into an interactive HTML prototype, deploys to Netlify, and returns the live URL.

**Request**

`Content-Type: application/json`

| Field | Type | Required | Description |
|---|---|---|---|
| `figma_url` | `string` | ✅ | Full Figma design/prototype URL, e.g. `https://www.figma.com/design/ABC123/My-App` |
| `figma_token` | `string` | ❌ | Figma Personal Access Token. Falls back to the server's `FIGMA_PERSONAL_ACCESS_TOKEN` env var if omitted. |

**Example request**
```json
{
  "figma_url": "https://www.figma.com/design/aBcDeFgHiJkL/My-App",
  "figma_token": "figd_..."
}
```

**Response `200`**
```json
{
  "url": "https://random-name-abc123.netlify.app",
  "site_id": "abc123-...",
  "deploy_id": "def456-...",
  "screens": 8,
  "nav_hotspots": 14,
  "toggles": 3,
  "timers": 1
}
```

| Field | Type | Description |
|---|---|---|
| `url` | `string` | Live Netlify URL of the deployed prototype |
| `site_id` | `string` | Netlify site ID |
| `deploy_id` | `string` | Netlify deploy ID |
| `screens` | `int` | Number of Figma frames converted |
| `nav_hotspots` | `int` | Number of navigation hotspots |
| `toggles` | `int` | Number of toggle interactions |
| `timers` | `int` | Number of timer-based transitions |

**Error responses**

| Status | When |
|---|---|
| `400` | Invalid Figma URL format, or no `figma_token` provided/configured |
| `422` | No frames found in the Figma file, or prototype validation failed |
| `502` | Figma API error, or Netlify site creation/deploy failed |
| `504` | Netlify deploy timed out (> 2 min) |

---

### 3. `POST /upload-html`

Direct HTML deploy — bypasses all Figma processing. Send a ready-made `.html` file (either as a direct upload or a Blob URL) and get back a live Netlify URL.

**Request**

`Content-Type: multipart/form-data`

Supply **exactly one** of the following form fields:

| Field | Type | Description |
|---|---|---|
| `file` | `file` | A `.html` file uploaded directly |
| `blob_url` | `string` | Vercel Blob URL that points to an `.html` file |

**Example — file upload**
```bash
curl -X POST https://prototype-pipeline.vercel.app/upload-html \
  -F "file=@my_prototype.html"
```

**Example — blob URL**
```bash
curl -X POST https://prototype-pipeline.vercel.app/upload-html \
  -F "blob_url=https://abc123.public.blob.vercel-storage.com/my_prototype.html"
```

**Example — JavaScript (fetch)**
```js
// File upload
const form = new FormData();
form.append("file", htmlFileBlob, "prototype.html");
const res = await fetch("https://prototype-pipeline.vercel.app/upload-html", {
  method: "POST",
  body: form,
});
const { url } = await res.json();

// Blob URL
const form = new FormData();
form.append("blob_url", "https://abc123.public.blob.vercel-storage.com/my.html");
const res = await fetch("https://prototype-pipeline.vercel.app/upload-html", {
  method: "POST",
  body: form,
});
const { url } = await res.json();
```

**Response `200`**
```json
{
  "url": "https://random-name-abc123.netlify.app",
  "site_id": "abc123-...",
  "deploy_id": "def456-..."
}
```

| Field | Type | Description |
|---|---|---|
| `url` | `string` | Live Netlify URL of the deployed prototype |
| `site_id` | `string` | Netlify site ID |
| `deploy_id` | `string` | Netlify deploy ID |

**Error responses**

| Status | When |
|---|---|
| `400` | Neither or both fields supplied; non-`.html` file; empty file; blob download failed |
| `502` | Netlify site creation or deploy failed |
| `504` | Netlify deploy timed out (> 2 min) |

---

## Notes

- Both `POST` endpoints are slow by design — they wait for the Netlify deploy to reach a `ready` state before responding (up to ~2 minutes). Plan for a long timeout on the frontend side.
- Each call creates a **brand new** Netlify site. There is no update/overwrite of an existing deployment.
- The interactive docs (Swagger UI) are available at [`/docs`](https://prototype-pipeline.vercel.app/docs).
