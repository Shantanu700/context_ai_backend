# Context-Aware Ad Recommender — backend

JSON REST API (Django + DRF). Ingests a video, understands each scene, recommends an ad
per scene with a rationale and a brand-safety flag. No frontend — Django admin is for
inspecting data during development.

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages Python 3.12 for you)
- Docker (Postgres + Redis)
- ffmpeg — needed from Phase 3 on: `brew install ffmpeg`

## Setup

```bash
uv sync
cp .env.example .env
docker compose up -d              # postgres (pgvector) on :5433, redis on :6380
uv run manage.py migrate
uv run manage.py createsuperuser  # optional, for /admin
uv run manage.py runserver
```

In a second terminal, the worker — all heavy work runs here, never in a view:

```bash
uv run celery -A config worker -l info
```

## Storage

`STORAGE_BACKEND=local` (default) writes under `MEDIA_ROOT` and serves it at `/media/`
while `DEBUG=True`. `STORAGE_BACKEND=r2` switches to Cloudflare R2 and hands out
presigned URLs; set `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_ENDPOINT_URL`. Nothing else in the code changes — the pipeline always pulls
objects to a local temp path before touching them.

## Sample video

Meridian (Netflix Open Content, CC BY 4.0) — ~850 MB, 4K HDR, no resume on the download:

```bash
uv run manage.py fetch_sample --process        # download, register, run the pipeline
uv run manage.py fetch_sample --path ./my.mp4  # or register a file you already have
```

Ports are 5433/6380 rather than the defaults because 5432/6379 were already in use
locally. To use the standard ports, edit `docker-compose.yml` and `DATABASE_URL`.

## Verify

```bash
curl localhost:8000/health        # {"status": "ok", "db": "ok"}
uv run manage.py test
open http://localhost:8000/admin/
```

## API documentation

| Path | What |
|---|---|
| `/schema` | OpenAPI 3.0.3, YAML. `?format=json` for JSON. Always served. |
| `/docs` | Swagger UI — **`DEBUG=True` only** |
| `/redoc` | Redoc — **`DEBUG=True` only** |

The two UIs are HTML, so they are dev-only inspection tools like the admin; the schema
itself is always available and is what the Next.js client should generate from:

```bash
curl localhost:8000/schema -o openapi.yaml
npx openapi-typescript openapi.yaml -o src/api.d.ts
```

Keep it honest in CI — this fails on any undocumented or mis-documented operation:

```bash
uv run manage.py spectacular --validate --fail-on-warn --file /dev/null
```

Swagger UI pulls its assets from a CDN, so `/docs` needs internet. Add
`drf-spectacular-sidecar` if you want it to work offline.

## Endpoints

| Method | Path | Status |
|---|---|---|
| GET | `/health` | live |
| POST | `/videos` | live — multipart `file` **or** JSON `source_url`, returns `202 {uuid, job_id}` |
| GET | `/videos/{uuid}/status` | live — `{status, scenes_done/scenes_total, progress}` |
| GET | `/videos/{uuid}/scenes` | live — tags, keyframes, transcript, recommended ad + rationale + safety flag |
| GET/POST | `/ads` | live |

```bash
curl -X POST localhost:8000/videos -F file=@clip.mp4
curl localhost:8000/videos/<uuid>/status
```

## Pipeline

`process_video` probes the file, then fans out with a Celery chord — scene detection and
transcription run in parallel, and the callback aligns them and persists the scenes:

```
process_video          pull to /tmp, ffprobe, reject over MAX_VIDEO_SECONDS
   |
   +-- detect_scenes   PySceneDetect ContentDetector -> cuts, then 1-3 keyframes
   |                   per scene (ffmpeg -ss, downscaled to <=768px) into storage
   +-- transcribe_audio  mono 16 kHz wav -> faster-whisper segments
   |                     (skipped, not failed, when there is no audio stream)
   |
   v
build_scenes           align segments to cuts by midpoint, persist Scene rows
   |
   +-- group of analyze_scene, one per scene (rate-limited)
   |     7. keyframes + transcript -> Gemini -> {description, objects, tone, iab_categories}
   |     8. scene embedding -> pgvector cosine vs the ad catalog, + IAB_BOOST per shared category
   |     9. top match -> Gemini one-line rationale + brand_safety_flag
   |
   v
finish_video           chord callback marks the video done
```

Every step runs in a worker — no view ever touches ffmpeg or Gemini. `scenes_done`
counts *attempts*: a scene Gemini chokes on is logged and skipped rather than stalling
the chord, and `scenes_failed` on the status endpoint reports how many ended untagged.

Knobs worth tuning: `WHISPER_MODEL` (default `base`; `tiny` is much faster and much
worse), `SCENE_THRESHOLD` (default 27, lower cuts more), `IAB_BOOST` (default 0.15 per
shared category) and `SCENE_ANALYSIS_RATE`.

## Ad catalog

```bash
uv run manage.py seed_ads --replace     # 12 ads, embedded locally
```

Ads created through `POST /ads` get embedded by a worker task; an ad with no embedding is
never matched. Gemini is restricted to a fixed IAB vocabulary (`core/gemini.py`) — with
free-form categories, scene and ad categories would never overlap and the boost would
be dead weight.

## Gemini quota

Default model is `gemini-flash-lite-latest`: cheapest of the Flash family and the one
with a workable free tier. `gemini-flash-latest` currently resolves to
`gemini-3.7-flash`, whose free tier is **20 requests per day** — at two calls per scene
that is ten scenes before it starts 429ing. `SCENE_ANALYSIS_RATE` (default `6/m`) keeps
the per-minute cap happy; raise it on a paid key.

## Running on macOS

Two platform quirks, both handled in settings, both worth knowing:

- The Celery worker uses the **threads** pool on Darwin. A forked child that loads torch
  aborts, because the Objective-C and OpenMP runtimes are not fork-safe.
- `EMBEDDING_DEVICE` defaults to **cpu**. sentence-transformers otherwise selects MPS,
  and torch's Metal kernels segfault when several worker threads drive them.

Videos arriving as `source_url` are downloaded directly; YouTube URLs shell out to
`yt-dlp` if it is on PATH (`uv add yt-dlp`) and otherwise fail with a message telling
you to upload a file or use the sample.
