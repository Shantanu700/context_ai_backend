# Deploying to Oracle Cloud Always Free

The stack is Supabase (Postgres + pgvector), Cloudflare R2 (media) and Gemini (AI), so the
only things that need a host are **web, worker and Redis**. One Always Free ARM VM runs
all three with room to spare.

Why this shape, measured on the real containers:

| | |
|---|---|
| worker peak RAM | 1107 MiB (torch + sentence-transformers + faster-whisper) |
| web peak RAM | 280 MiB |
| redis | 20 MiB |
| Oracle A1 Always Free | 4 ARM cores, 24 GB RAM — fits ~15× over |
| GCP e2-micro Always Free | 1 GB RAM — the worker alone does not fit |

Redis stays on the box. An idle Celery worker issues ~133 Redis commands/minute (mostly
`brpop` polling), which is 5.7M/month — Upstash's free tier is 500k, so a managed
per-command Redis is exhausted in under three days while doing nothing.

torch, opencv, ctranslate2, onnxruntime, numpy and av all publish `aarch64` wheels, so ARM
needs no changes to `pyproject.toml`.

---

## 1. Create the instance

Oracle Cloud console → **Compute → Instances → Create instance**.

- **Shape**: Ampere **VM.Standard.A1.Flex**, 4 OCPU / 24 GB (the whole free allowance)
- **Image**: Canonical Ubuntu 24.04
- **Region**: pick one near Supabase — yours is `ap-southeast-1` (Singapore), so use
  Singapore or Mumbai. Compute in the US would add ~200 ms to every database query.
- **Boot volume**: raise to **100 GB** (free tier allows 200 GB total). The image is
  5.5 GB and the build cache wants room.
- Save the SSH private key it offers. You cannot re-download it.

> **"Out of host capacity"** is the usual first result — A1 is heavily contested. Retry,
> switch availability domain, or try another nearby region. It is a stock problem, not a
> configuration error.

## 2. Open the port

Two layers, and **missing the second is the classic Oracle mistake** — the console shows
the port as open while the instance itself still drops the traffic.

**a. Security List** — VCN → Subnet → Security List → Add Ingress Rule:
Source `0.0.0.0/0`, TCP, destination ports `80,443`.

**b. The instance firewall** — Ubuntu images ship iptables rules that reject everything
except SSH:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 3. Install Docker

```bash
ssh -i your-key.pem ubuntu@<public-ip>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && exit      # log back in for the group to apply
```

## 4. Deploy

```bash
git clone <your-repo> context-ai && cd context-ai/backend
cp .env.prod.example .env.prod && nano .env.prod
```

Fill in — these are the ones that must change from the example:

```ini
SECRET_KEY=                 # python3 -c "import secrets; print(secrets.token_urlsafe(50))"
ALLOWED_HOSTS=api.yourdomain.com
CSRF_TRUSTED_ORIGINS=https://api.yourdomain.com
CORS_ALLOW_ALL_ORIGINS=False
CORS_ALLOWED_ORIGINS=https://your-frontend.vercel.app
DATABASE_URL=               # Supabase session pooler URI
STORAGE_BACKEND=r2
R2_BUCKET=context-ai-bucket
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
GEMINI_API_KEY=
```

Then build and start. The first build downloads torch and bakes the model weights, so
expect **15–25 minutes** on 4 ARM cores:

```bash
docker compose -f docker/prod/docker-compose.yaml --env-file .env.prod up -d --build
docker compose -f docker/prod/docker-compose.yaml --env-file .env.prod exec web python manage.py migrate
docker compose -f docker/prod/docker-compose.yaml --env-file .env.prod exec web python manage.py seed_ads
docker compose -f docker/prod/docker-compose.yaml --env-file .env.prod exec web python manage.py createsuperuser
```

Only `web`, `worker` and `redis` start — Postgres is Supabase. (`--profile localdb` adds a
local Postgres if you ever want one.)

## 5. TLS

Gunicorn should not face the internet directly. Either option is free; **Caddy** needs
port 80/443 open, **Cloudflare Tunnel** needs no inbound ports at all.

**Caddy** — point an A record at the instance IP first, then:

```bash
sudo apt install -y caddy
echo 'api.yourdomain.com {
    reverse_proxy localhost:8000
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

Caddy obtains and renews the certificate automatically and sets `X-Forwarded-Proto`,
which `SECURE_PROXY_SSL_HEADER` in settings relies on.

**Cloudflare Tunnel** — no open ports, no certificate management:

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o cloudflared && chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/
cloudflared tunnel login
cloudflared tunnel create context-ai
cloudflared tunnel route dns context-ai api.yourdomain.com
cloudflared tunnel run --url http://localhost:8000 context-ai
```

## 6. Verify

Run these in order; each one fails loudly if its dependency is wrong.

```bash
# storage: writes, reads, presigns, fetches anonymously, deletes
docker compose -f docker/prod/docker-compose.yaml --env-file .env.prod exec web \
  python manage.py check_storage

# security checklist (1 silenced: CSRF, see settings.py)
docker compose -f docker/prod/docker-compose.yaml --env-file .env.prod exec web \
  python manage.py check --deploy

curl https://api.yourdomain.com/health          # {"status":"ok","db":"ok"}

curl -c jar -X POST https://api.yourdomain.com/login \
     -H "Authorization: Basic $(printf 'user:pass' | base64)"
curl -b jar https://api.yourdomain.com/ads      # 12 seeded ads

curl -b jar -X POST https://api.yourdomain.com/videos -F file=@clip.mp4
curl -b jar https://api.yourdomain.com/videos/<uuid>/status
```

A successful run ends at `done | N/N scenes analyzed | failed: 0`, and
`/videos/<uuid>/scenes` returns keyframe URLs on `r2.cloudflarestorage.com`.

## 7. Operations

```bash
# logs
docker compose -f docker/prod/docker-compose.yaml --env-file .env.prod logs -f worker

# deploy a change (the prod image bakes the source in, so rebuild)
git pull && docker compose -f docker/prod/docker-compose.yaml --env-file .env.prod up -d --build

# reclaim disk after a few rebuilds — old 5.5GB images add up
docker image prune -af
```

Containers use `restart: unless-stopped`, so they survive a reboot once Docker is enabled
at boot (`sudo systemctl enable docker`).

Nothing on this box holds state worth backing up: Postgres is Supabase, media is R2, and
Redis only carries in-flight jobs. Losing the VM costs a rebuild, not data.

## Things that will bite

- **Gemini free tier** caps at 15 requests/minute, and each scene costs two calls.
  `SCENE_ANALYSIS_RATE=4/m` respects that; a 12-minute video is then roughly an hour.
  Raise it on a paid key — it is the single biggest lever on throughput.
- **Keep the worker at `--concurrency=1`.** The rate limit is enforced per worker, so a
  second one doubles the request rate into Gemini and earns 429s.
- **Supabase free pauses after ~1 week idle.** A paused project means a failed demo until
  you unpause it in the dashboard.
- **`MAX_VIDEO_SECONDS` defaults to 300.** Raise it before uploading anything longer.
- **First request after a deploy is slow** — gunicorn imports torch lazily. The
  healthcheck's `start-period` covers it.
