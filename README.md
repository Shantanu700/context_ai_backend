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
| POST | `/videos` | phase 2 |
| GET | `/videos/{uuid}/status` | phase 2 |
| GET | `/videos/{uuid}/scenes` | phase 3 |
| GET/POST | `/ads` | phase 2 |
