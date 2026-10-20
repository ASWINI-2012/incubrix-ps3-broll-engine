# IncuBrix AI Script-to-B-roll Video Engine

A full-stack project for turning a script, audio narration, or manually planned scenes into narrated MP4 videos. The backend is FastAPI/Python; the frontend is React/Vite. SQLite persists job state. Footage is supplied through Wikimedia Commons, Internet Archive, the local catalogue, and optional Pixabay/Pexels providers; script narration uses installed local voices through `pyttsx3`, and FFmpeg/FFprobe render and validate output.

## Requirements

- Python 3.11 or newer (the current workspace has Python 3.13.3).
- Node.js 20 or newer and npm.
- FFmpeg and FFprobe on `PATH`, or set `FFMPEG_PATH` and `FFPROBE_PATH`.
- No footage API key is needed. Automatic mode queries Wikimedia Commons and Internet Archive, then uses eligible local clips as a fallback. Optional Pixabay/Pexels integrations still use their API keys.
- An installed system speech voice for local script narration. For audio-only transcription, install the optional `faster-whisper` dependency; the first model run downloads the configured model.

The default `BROLL_PROVIDER=auto` searches Wikimedia Commons and Internet Archive independently without credentials. If Pixabay or Pexels credentials are configured, those sources are queried too. Each candidate must have explicit source, creator, recognized licence, required attribution terms, relevant source metadata, a downloadable video response, and a valid video stream. Downloads that pass validation are kept in a managed cache. The local catalogue is the fallback after a small set of public provider queries. Empty or unavailable sources are reported; unrelated clips are never substituted. Audio-only requests need optional transcription support or supplied manual scenes.

On Windows, install FFmpeg with:

```powershell
winget install --id Gyan.FFmpeg --exact --scope user --accept-source-agreements --accept-package-agreements
```

Restart VS Code or open a new terminal after installation so the updated `PATH` is loaded. If the executables are not found, set `FFMPEG_PATH` and `FFPROBE_PATH` to their absolute paths in `.env`.

## Setup

Open two terminals in the project folder.

### Backend

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

The copied `.env.example` defaults to automatic provider selection. Public source search requires no key. Ensure `ffmpeg -version` and `ffprobe -version` work in the same terminal. Set a different `FFMPEG_PATH`/`FFPROBE_PATH` in `.env` if required.

### Local demo mode

Set `BROLL_PROVIDER=demo` in `.env` for offline/local-catalogue mode. Scan and add relevant validated clips through Media Library. The app refuses to use a scene with no relevant match instead of rendering a black or unrelated clip. Narration and captions remain local. The historical bundled NASA file is not selected because it contains black title cards and unrelated text. `MIN_FOOTAGE_RELEVANCE` sets the minimum metadata overlap; `MAX_BLACK_FRAME_FRACTION` rejects rendered footage that is mostly black before captions are added.

### Public footage sources

Wikimedia Commons requires `MEDIA_API_USER_AGENT` to identify the application and include a real operator contact (email or website). Configure it in `.env`; Commons search is skipped if it is missing. A Commons 403 pauses further Commons requests for that job and is not bypassed. Internet Archive remains independently searchable. Public-source relevance is based on titles and descriptions; broad subject tags are retained as provenance but are not enough to establish a scene match.

### AI-generated image scenes

The Studio's **Visual source → AI-generated images** option makes one new image per scene through the local AUTOMATIC1111 Stable Diffusion WebUI API. It does not call stock footage providers or use the local video catalogue. Narration stays intact; each scene gets its own prompt, image, seed, and entry in `image_generation_report.json`. Images are validated and retained under that job's `generated_images/` directory. FFmpeg applies a slow zoom, dissolves between scenes, and combines the visuals with narration and captions. The manifest and timeline identify the backend, model, prompts, seeds, and image paths. Generated-model licensing and content rights still need review; the rights report does not mark them approved.

Image generation is optional and deliberately fails with an actionable error until configured. Install and download a model separately; this project does not silently fetch multi-gigabyte weights. On this development machine, no local image model or diffusion runtime was found. The detected NVIDIA GeForce RTX 2050 has 4 GB VRAM. A Stable Diffusion 1.5 checkpoint at 512-pixel base resolution is a plausible local option; expect slow generation, possible out-of-memory errors, and use the WebUI's `--medvram` or `--lowvram` setting. AUTOMATIC1111's own guide describes these approaches for 4 GB GPUs and its Windows setup uses Python 3.10 plus Git. Its model/checkpoint files are multi-gigabyte; allow roughly 10 GB or more free disk for the WebUI, Python dependencies, model, and cache (an estimate, not a vendor minimum). The WebUI API is enabled with `--api` and documents `/sdapi/v1/txt2img` and `/sdapi/v1/options`.

Configure the app environment:

```env
IMAGE_GENERATION_BACKEND=automatic1111
IMAGE_GENERATION_URL=http://127.0.0.1:7860
IMAGE_GENERATION_MODEL=
IMAGE_GENERATION_RESOLUTION=512
IMAGE_GENERATION_STEPS=20
IMAGE_GENERATION_TIMEOUT_SECONDS=300
```

If the backend runs on another machine, set its reachable URL and optional `IMAGE_GENERATION_API_KEY`; never expose an unauthenticated image server to an untrusted network. The app expects the AUTOMATIC1111-compatible `/sdapi/v1/options` and `/sdapi/v1/txt2img` endpoints. [AUTOMATIC1111 API documentation](https://github.com/AUTOMATIC1111/stable-diffusion-webui/wiki/API), [NVIDIA installation guide](https://github.com/AUTOMATIC1111/stable-diffusion-webui/wiki/Install-and-Run-on-NVidia-GPUs), and [low-VRAM troubleshooting](https://github.com/AUTOMATIC1111/stable-diffusion-webui/wiki/Troubleshooting#low-vram-video-cards).

`auto` (or `BROLL_PROVIDER=public`) queries both public APIs for each scene. Wikimedia Commons uses the MediaWiki Action API search generator and `imageinfo` extended metadata; Internet Archive uses Advanced Search followed by its item metadata endpoint. Results are capped per search and only the selected candidate is downloaded. The implementation accepts known CC BY, CC BY-SA, CC0, and Public Domain Mark URLs; unknown or absent licence/creator details are rejected. Review the actual source page and licence before distributing rendered work, including third-party rights for people, brands, and property.

The integrations follow the official [MediaWiki Search API](https://www.mediawiki.org/wiki/API:Search), [MediaWiki Imageinfo API](https://www.mediawiki.org/wiki/API:Imageinfo), [Internet Archive Search API documentation](https://archive.org/developers/search.html), and [Internet Archive Metadata API](https://archive.org/developers/metadata.html). Public APIs may return no suitable asset for a particular scene; the job then reports the unresolved scene instead of forcing a render.

### Optional keyed footage providers

Optional Pixabay/Pexels providers are queried by auto mode when their API keys are configured. Or select one explicitly. Never commit `.env` or paste API keys into source code.

```env
BROLL_PROVIDER=pixabay
PIXABAY_API_KEY=your_pixabay_key
```

Supported choices are `auto`, `public`, `pixabay`, `pexels`, and `demo`. Pixabay's official [video API documentation](https://pixabay.com/api/docs/) specifies `GET https://pixabay.com/api/videos/` and returns source-page URLs, creator names, tags, and downloadable video variants. Its [Content License](https://pixabay.com/service/terms/) permits adaptation into a new creative work, prohibits standalone redistribution, and warns that additional permissions may be needed for people, brands, or property shown. Pexels' [video API documentation](https://www.pexels.com/api/documentation/) documents `https://api.pexels.com/v1/videos/search` and requires an `Authorization` key.

Pixabay API keys are supplied through `PIXABAY_API_KEY`. The integration caches search responses per job, uses returned tags for relevance checks, and downloads the selected video file only after validating the source, licence metadata, HTTP response, media type, and FFprobe result. Review the source item for model, property, and trademark rights required by your intended use.

```env
BROLL_PROVIDER=pexels
PEXELS_API_KEY=your_pexels_key
```

The app queries the configured providers first and falls back to eligible local catalogue footage. Missing credentials, unsuitable candidates, or provider failures produce a clear failed job; the engine does not claim that unrelated footage matched.

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

The API is at `http://127.0.0.1:8000`; interactive API docs are at `http://127.0.0.1:8000/docs`.

### Frontend

```powershell
cd frontend
npm ci
npm run dev
```

Open the Vite URL printed in the terminal (normally `http://localhost:5173`). Set `VITE_API_URL` if the backend runs elsewhere.

The IncuBrix Studio interface includes a dashboard, script/audio/scene creation studio, locally remembered projects, generated-video library, and backend settings. Project history is limited to job IDs known to the current browser because the API does not expose an account-wide listing endpoint.

### Local media dataset

The media root is `MEDIA_LIBRARY_DIR` (default `./data/media_library`) and the catalogue is `clips.json` in that directory. Put MP4/MOV/WebM clips and JPG/PNG/WebP images there, optionally in category subfolders. Import or rescan repeatedly with:

```powershell
.\.venv\Scripts\python.exe -m backend.app.media_cli import
.\.venv\Scripts\python.exe -m backend.app.media_cli validate --healthcare
```

The importer validates videos and images with FFprobe, computes a SHA-256 duplicate key, and updates the existing catalogue without duplicating a previously imported path. A filename and parent category can contribute neutral search terms; they are not treated as verified visual descriptions. To provide actual description and rights details, add a sidecar named `clip.mp4.json` next to `clip.mp4` (same naming rule for images). Example:

```json
{
  "category": "Healthcare and medicine",
  "caption": "Patient speaking with a doctor by video call",
  "description": "Telemedicine consultation",
  "tags": ["telemedicine", "doctor", "patient", "video consultation"],
  "source_url": "https://www.pexels.com/video/actual-clip-page/",
  "provider": "Pexels",
  "creator": "Creator name shown on the source page",
  "license_name": "Pexels License",
  "license_url": "https://www.pexels.com/license/",
  "attribution_required": "Not required by the Pexels License; credit appreciated.",
  "usage_rights_confirmed": true
}
```

Only enter details confirmed on the asset's source page and applicable licence. Missing source/provider/licence or an explicit `usage_rights_confirmed: true` is retained as unknown and marked `review_required`; those items are excluded from scene matching until corrected. Images are catalogued and validated but the current scene renderer only selects video clips. The optional healthcare catalogue validation command reports technically valid, rights-reviewed clip totals and named scenes still missing suitable metadata matches; it does not define or inject scenes into user scripts.

For a small healthcare demo set, browse Pexels' [healthcare video search](https://www.pexels.com/search/videos/health%20care%20education/), download only a few clips whose content actually fits the target scenes, and retain their individual page URL, creator, and displayed licence details in each sidecar. Pexels says its licence permits free use and does not require attribution, while restricting standalone redistribution, misleading endorsement, and harmful/offensive portrayal of identifiable people; review the [current Pexels licence](https://www.pexels.com/license/) and depicted third-party rights before use. Do not simply redistribute the downloaded source files as a stock library. This manual import requires no API key. The optional API collector remains explicit and bounded by `MEDIA_MAX_*`; preview with `POST /api/v1/media/download?dry_run=true` and collect with `dry_run=false`. It uses the official API, not scraping, and reports missing API credentials.

## Run tests and build

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The full suite includes local FFmpeg/FFprobe render tests. If either executable is not on `PATH`, set `FFMPEG_PATH` and `FFPROBE_PATH` to their absolute executable paths in the current terminal before running tests.

```powershell
cd frontend
npm run build
npm test
```

The frontend tests exercise the centralized API client and its job/error handling.

## Workflow and outputs

1. Submit a script, MP3/WAV/M4A upload, HTTPS audio URL, or manual scene list.
2. Poll the returned job ID. The job moves through audio preparation, scene planning, footage retrieval, and rendering.
3. On success, preview or download the single MP4 for the selected aspect ratio. A single MP4 can contain multiple relevant B-roll scenes from that render. Each completed job also contains `captions.srt`, `timeline.json`, `manifest.json`, and the generated/preserved narration.

Each request selects exactly one aspect ratio and creates one filesystem-safe MP4: `16:9` → `video_16x9.mp4`, `9:16` → `video_9x16.mp4`, or `1:1` → `video_1x1.mp4`. The output canvas is 1920×1080, 1080×1920, or 1080×1080 at 30 fps, H.264/AAC. Source clip dimensions and provider/license links are retained in the timeline; a rendered canvas size does not imply that source footage had that resolution.

For local container development, copy `.env.example` to `.env` and run `docker compose up --build` for automatic provider selection. Public footage search works without a key; optional Pixabay/Pexels credentials can be added in `.env`. The API is published on port 8000 and the Vite frontend on 5173. Compose persists SQLite data and finished outputs in named Docker volumes.

For JSON API calls use `POST /api/v1/broll/jobs`. It accepts a script, manual scenes, or audio URL with optional manual scenes. Audio uploads use `multipart/form-data` with an `audio` field, and may include scenes. Supply `Idempotency-Key` to deduplicate retries. Job, result, timeline, and output-file routes are under `/api/v1/jobs/{job_id}`. `/health` checks process liveness. `/ready` checks rendering tools and reports the selected footage provider and credential status; a missing provider key does not prevent app startup.

### Audio-only transcription

Audio transcription is optional and deliberately not installed in the default dependency set because its model/runtime is comparatively large. To enable it, install `faster-whisper` in the active virtual environment. The default model is `tiny` on CPU; set `WHISPER_MODEL` to another supported model if needed. Without it, audio-only jobs fail with a clear `transcription_unavailable` error; upload audio with manual scene text to skip transcription.

### Security and licensing notes

- API credentials are read from environment variables and are not returned by API routes.
- Audio URL downloads require HTTPS, reject DNS results resolving to local/private IPs, and do not follow redirects. Upload size and audio duration are bounded.
- Provider requests and downloads have timeouts and file-size limits. Selected provider video bytes are cached under `FOOTAGE_CACHE_DIR` after FFprobe validation; each new search still revalidates source and rights metadata before cache reuse. Pixabay search responses remain scoped per job. Each asset's actual source, provider, licence, attribution note, and matching method are recorded. Relevance uses provider-supplied titles/descriptions/tags and does not inspect the full clip frame-by-frame; review source footage and rights before publication.
- Local imports with incomplete provenance/licence details are marked for manual review and are not eligible for footage selection. Review requirements for identifiable people, brands, and other depicted content still apply even when a stock licence permits reuse.
- Avoid terms are removed from search terms and matched against source-page metadata; the initial keyword implementation does not inspect clip pixels and cannot guarantee exclusion of visual content absent from provider metadata.
- The initial job worker is an in-process thread pool. SQLite retains job records, but running work is marked interrupted after a server restart. This is for a local assessment/demo and is not a distributed production queue.

## OpenAPI

The live FastAPI OpenAPI JSON is at `/openapi.json`; the maintained specification is [openapi.yaml](./openapi.yaml).
