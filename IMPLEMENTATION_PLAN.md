# Implementation plan

## Current project status

- The project directory was empty at inspection time; there was no README, source code, tests, OpenAPI file, environment configuration, Docker setup, or assessment PDF to preserve.
- Python 3.13.3, Node.js 24.21.0, npm 11.19.0, Docker 29.1.3, and Git 2.55.0 are available.
- FFmpeg/FFprobe 9.0.2 are now installed with Winget in user scope. The PATH update is not visible to the current VS Code process, so validation used their installed executable paths; restart VS Code or set `FFMPEG_PATH`/`FFPROBE_PATH`.
- No Pexels API key is available in the project. Production footage retrieval must be configured by the user.
- The FastAPI/React application, SQLite job store, media pipeline, OpenAPI specification, tests, Dockerfiles, and Compose configuration are implemented.

## Implemented phases

### Phase 1 — Runnable end-to-end foundation (implemented)

- FastAPI and responsive React/Vite UI with script, manual-scene, audio-upload, and HTTPS audio-URL inputs.
- SQLite job/idempotency persistence, in-process asynchronous worker, status/result/timeline/file routes, request IDs, and health/readiness checks.
- Local `pyttsx3` narration, Pexels retrieval/ranking, aspect-ratio rendering, FFprobe validation, SRT captions, timeline, and provenance manifest.
- Dockerfiles and Compose configuration.

### Phase 2 — Run and verify (completed; see verification summary)

- Backend tests cover request validation, idempotency, media handling, timing/captions, URL safety, clip matching, and an FFmpeg render/pipeline with synthetic local fixtures.
- Frontend production bundle builds successfully.
- `openapi.yaml` parses as OpenAPI 3.1 YAML and live FastAPI schema generation is tested.
- A local Windows speech voice generated a valid WAV in a direct smoke test.
- A real Pexels-backed job was not run because no API key is configured; no synthetic fixture is represented as a production asset.

### Phase 3 — Optional assessment/deployment improvements (remaining)

- Compare implementation against the original assessment PDF if it becomes available.
- Run Docker Compose in a configured Docker environment; it was not started during this implementation.
- Install optional `faster-whisper` for audio-only transcription. Manual scenes can accompany audio without it.
- Add sentence-transformer embeddings, provider fallback/alternate-query retries, and a distributed durable queue if required.

## Verification results

- Backend: **21 passed** using Python 3.13.3 and FFmpeg/FFprobe 9.0.2 executable paths. Coverage includes real local FFmpeg/FFprobe fixture rendering for landscape, portrait, and square outputs, the end-to-end pipeline, multipart upload, and mocked Pexels query/avoid-term behavior. Pytest reports one Starlette/httpx deprecation warning.
- Frontend: `npm run build` succeeded with Vite 6.4.4.
- API contract: `openapi.yaml` parsed successfully as OpenAPI 3.1.0 with eight paths.
- Local TTS: Windows system voice smoke test generated a valid WAV.
- Not exercised: Docker Compose startup and a live Pexels-backed production job. The latter needs a user-provided Pexels API key.

## Acceptance tests

- [x] Script, scene, setting, URL, and upload validation rejects malformed/unsupported input.
- [x] Jobs persist, can be polled, expose results/timelines, and reuse an idempotency key only for the same request.
- [x] Scene timing spans the narration; SRT captions are wrapped, timed, and non-overlapping.
- [x] Clip selection is deterministic, avoids reuse when alternatives exist, and records provider/source/license metadata.
- [x] FFmpeg/FFprobe fixture integration produces H.264/AAC MP4s for all three requested aspect ratios.
- [x] The React production build succeeds and API schema generation is checked.
- [ ] Verify a real Pexels-backed job after configuring the user's API key.

## Known limitations

- The workspace was empty and the assessment PDF was not present, so requirements below are based on the supplied project prompt only.
- Pexels API credentials must be configured before a real stock-footage job can complete. Without credentials, the backend starts and automated tests run, but production jobs fail explicitly at footage retrieval.
- Local TTS depends on a speech engine installed on the host. The initial implementation uses `pyttsx3`; supported voices vary by operating system.
- SQLite and an in-process worker pool are suitable for a local assessment/demo, not a horizontally scaled production deployment. Jobs survive restarts, but queued/running work is marked failed on restart.
- Clip ranking uses deterministic keyword overlap on scene text/source-page metadata, with Pexels search relevance as a tie-break; sentence-transformer embeddings and a secondary provider are not included.
- Audio-only transcription requires the optional `faster-whisper` package/model. Audio uploads/URLs with manual scene text do not require it.
- Avoid terms are applied to search words and provider metadata; clip pixels are not classified, so visual exclusions are not guaranteed.
