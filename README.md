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

## Endpoints

| Method | Path | Status |
|---|---|---|
| GET | `/health` | live |
| POST | `/videos` | live — multipart `file` **or** JSON `source_url`, returns `202 {uuid, job_id}` |
| GET | `/videos/{uuid}/status` | live — `{status, scenes_done/scenes_total, progress}` |
| GET | `/videos/{uuid}/scenes` | phase 3 |
| GET/POST | `/ads` | live |

```bash
curl -X POST localhost:8000/videos -F file=@clip.mp4
curl localhost:8000/videos/<uuid>/status
```

The pipeline task is still a stub through Phase 2: it pulls the file back out of storage
to prove the round-trip, then ticks a fake 5-scene counter. Real media work lands in Phase 3.
