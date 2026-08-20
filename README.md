# Context-Aware Ad Recommender — backend

A JSON REST API that ingests a video, understands each scene (visuals, speech, emotional
tone) and recommends which ad from a catalog to place at which timestamp — each with a
one-line generated rationale and a brand-safety flag.

Backend only. No frontend; the Django admin is for inspecting data during development.

```
POST /videos ──► Celery ──► ffprobe ─► scene detection ─► keyframes ─┐
                                   └─► audio ─► whisper ────────────┤
                                                                     ▼
                        Gemini tags ─► embed ─► pgvector match ─► rationale + safety flag
```

## Requirements

- [uv](https://docs.astral.sh/uv/) — manages Python 3.12 for you
- Docker — Postgres (pgvector) and Redis
- ffmpeg — `brew install ffmpeg`
- A Gemini API key — https://aistudio.google.com/apikey

## Setup

```bash
uv sync
cp .env.example .env          # then put your GEMINI_API_KEY in it
docker compose up -d          # postgres :5433, redis :6380
uv run manage.py migrate
uv run manage.py seed_ads     # 12 ads, embedded locally
uv run manage.py createsuperuser   # optional, for /admin
```

Two terminals:

```bash
uv run manage.py runserver                  # API on :8000
uv run celery -A config worker -l info      # all heavy work happens here
```

Ports are 5433/6380 rather than the defaults because 5432/6379 were already in use on the
development machine. Change `docker-compose.yml` and `DATABASE_URL`/`REDIS_URL` to suit.

## The sample: Meridian

[Meridian](http://download.opencontent.netflix.com/) is a 12-minute film-noir short that
Netflix publishes as Open Content under CC BY 4.0 — real cuts, real mood shifts, and a
stable URL, so the demo never depends on YouTube. Its MP4 is 851 MB of 4K HDR and,
worth knowing, **has no audio track** (the Atmos audio ships as separate files), so
transcripts come back empty and the recommendations rest on visuals alone.

One command — download, register, process:

```bash
uv run manage.py fetch_sample --trim 120 --process
curl localhost:8000/videos/<uuid>/status
curl localhost:8000/videos/<uuid>/scenes
```

`--trim 120` registers only the opening two minutes (stream copy, no re-encode). The full
12 minutes is roughly 200 scenes, so ~400 Gemini calls — see [Gemini quota](#gemini-quota)
before running it, and raise `MAX_VIDEO_SECONDS` to 900 since the default cap is 300.

Already downloaded it, or want a different video?

```bash
uv run manage.py fetch_sample --path ~/Downloads/whatever.mp4 --process
```

Measured on the 2-minute excerpt: 25 scenes, 34 keyframes, 0 failures, about 11 minutes
end to end — nearly all of it waiting on the free-tier Gemini rate limit.

## Inspecting frame extraction

To see which frames the pipeline pulls, without a database, Celery or Gemini quota:

```bash
uv run manage.py preview_frames .samples/Meridian_UHD4k5994_HDR_P3PQ.mp4 --end 90
open frames_preview/
```

JPEGs are named `scene_frame_timestamp.jpg`. Useful for tuning `--threshold`; `--start`
and `--end` take seconds or `HH:MM:SS` and let you sample a window of a long file.

## API

| Method | Path | |
|---|---|---|
| GET | `/health` | liveness + DB probe |
| POST | `/videos` | multipart `file` **or** JSON `source_url`; returns `202 {uuid, job_id}` |
| GET | `/videos/{uuid}/status` | `{status, scenes_done/scenes_total, scenes_failed, progress}` |
| GET | `/videos/{uuid}/scenes` | scenes with tags, keyframe URLs, recommended ad, rationale, safety flag |
| GET, POST | `/ads` | the catalog |
| GET | `/schema` | OpenAPI 3.0.3 (YAML; `?format=json` for JSON) |
| GET | `/docs`, `/redoc` | Swagger UI / Redoc — **`DEBUG=True` only**, they are HTML |

```bash
curl -X POST localhost:8000/videos -F file=@clip.mp4
curl -X POST localhost:8000/videos -H 'Content-Type: application/json' \
     -d '{"source_url":"https://example.com/clip.mp4"}'
```

Generate a typed client from the schema:

```bash
curl localhost:8000/schema -o openapi.yaml
npx openapi-typescript openapi.yaml -o src/api.d.ts
```

CI can hold the docs honest — this fails on any undocumented or mis-documented operation:

```bash
uv run manage.py spectacular --validate --fail-on-warn --file /dev/null
```

## Pipeline

Views never touch ffmpeg or Gemini; everything below runs in a worker.

```
process_video          pull to /tmp, ffprobe, reject over MAX_VIDEO_SECONDS
   |
   +-- detect_scenes     PySceneDetect ContentDetector -> cuts, then 1 keyframe for a
   |                     scene under 5s, 2 under 15s, else 3, spaced off the cut points
   |                     and downscaled to <=768px, uploaded to storage
   +-- transcribe_audio  mono 16 kHz wav -> faster-whisper segments
   |                     (skipped, not failed, when there is no audio stream)
   |
   v
build_scenes           align segments to cuts by midpoint, persist Scene rows
   |
   +-- group of analyze_scene, one per scene, rate-limited
   |     keyframes + transcript -> Gemini -> {description, objects, tone, iab_categories}
   |     scene embedding -> pgvector cosine over the ad catalog, + IAB_BOOST per shared category
   |     top match -> Gemini one-line rationale + brand_safety_flag
   |
   v
finish_video           chord callback marks the video done
```

`scenes_done` counts *attempts*, incremented with `F()` so concurrent tasks cannot lose
one. A scene that hits a rate limit is re-queued; one that fails for any other reason is
logged and left untagged rather than stranding the chord, and `scenes_failed` on the
status endpoint reports how many ended that way.

## Ad catalog and matching

```bash
uv run manage.py seed_ads --replace
```

Each ad is embedded locally with sentence-transformers. Ads created through `POST /ads`
are embedded by a worker task; an ad without an embedding is never matched.

Gemini is restricted to a fixed IAB vocabulary (`core/gemini.py`). With free-form
categories, scene and ad categories would essentially never coincide and `IAB_BOOST`
would be dead weight. Ranking is cosine similarity plus `IAB_BOOST` per shared category,
so a slightly more distant ad in the right category beats a closer one in the wrong one.

## Storage

`STORAGE_BACKEND=local` (default) writes under `MEDIA_ROOT`, served at `/media/` while
`DEBUG=True`. `STORAGE_BACKEND=r2` switches to Cloudflare R2 and hands out presigned
URLs; set `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT_URL`.
Nothing else changes — the pipeline always pulls objects to a local temp path first.

## Gemini quota

Free-tier limits are the main brake on a long video. The default model
`gemini-flash-lite-latest` allows **15 requests per minute**, and at two calls per scene
`SCENE_ANALYSIS_RATE=4/m` leaves headroom for retries — which spend the same budget, so
running closer to the cap cascades into 429s. `gemini-flash-latest` is better at vision
but currently resolves to a model with a **20 requests per day** free tier.

On a paid key, raise `SCENE_ANALYSIS_RATE` and the whole thing gets much faster.

## Configuration

Everything is env-driven via `django-environ`; see `.env.example`.

| Variable | Default | |
|---|---|---|
| `MAX_VIDEO_SECONDS` | `300` | longer uploads are rejected |
| `SCENE_THRESHOLD` | `27.0` | ContentDetector sensitivity; lower cuts more |
| `WHISPER_MODEL` | `base` | `tiny` is much faster and much worse |
| `GEMINI_MODEL` | `gemini-flash-lite-latest` | |
| `SCENE_ANALYSIS_RATE` | `4/m` | scenes per minute |
| `TOP_K_ADS` / `IAB_BOOST` | `3` / `0.15` | ranking |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | MiniLM-L6-v2 / `384` | must agree, or migrations reject the vectors |
| `EMBEDDING_DEVICE` | `cpu` | see below |
| `STORAGE_BACKEND` | `local` | or `r2` |

## Running on macOS

Two platform quirks, both handled in settings, both worth knowing about:

- The Celery worker uses the **threads** pool on Darwin. A forked child that loads torch
  aborts — the Objective-C and OpenMP runtimes are not fork-safe.
- `EMBEDDING_DEVICE` defaults to **cpu**. sentence-transformers otherwise selects MPS,
  and torch's Metal kernels segfault when several worker threads drive them.

You will also see an `objc[...] Class AVFFrameReceiver is implemented in both` warning:
OpenCV and PyAV each bundle their own `libavdevice`. It is noise, not a fault.

## Tests

```bash
uv run manage.py test
```

33 tests. The ffmpeg ones build real clips and probe them; the Gemini ones are mocked, so
the suite costs no quota and needs no API key.

## Layout

```
config/        settings, celery app, root urls
core/
  models.py       Video, Scene, Ad
  tasks.py        the Celery pipeline
  media.py        ffprobe / scenedetect / keyframes / whisper — pure functions, local paths
  gemini.py       scene analysis + rationale, and the IAB vocabulary
  embeddings.py   local sentence-transformers
  matching.py     pgvector similarity + IAB boost
  storage.py      store / pull_to_tmp / download
  views.py        DRF viewsets
  management/commands/   seed_ads, fetch_sample, preview_frames
```
