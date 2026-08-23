# Running & Deploying jobharvestingss

Two parts to this document:
1. **[Run and test locally](#1-run-and-test-locally)** — on Windows, with or without Docker.
2. **[Deploy to AWS (EC2)](#2-deploy-to-aws-ec2)** — the production target.

## Architecture at a glance

```
                         EC2 instance (or local Docker/WSL2)
 Browser  ──HTTP(S)──▶  nginx (frontend container)
                              ├─ /                → React build (static)
                              ├─ known API routes  → FastAPI backend :8000
                              └─ /vnc/             → websockify :6080 (live browser view)
                                                          │
                                               x11vnc  ──▶ Xvfb :99  ◀── Chromium (headless=False)
                                                                          (persistent Chrome profile)
```

- Three services: `postgres` (PostgreSQL 16, the app's database — named volume `pgdata`), `api` (FastAPI + Playwright + Xvfb/x11vnc/websockify, all under `supervisord`) and `frontend` (nginx serving the React build and reverse-proxying the rest). See `POSTGRES_MIGRATION.md` for the SQLite→PostgreSQL migration details.
- One compose file (repo root) and one data directory: `ai-harvest-agent/data` (Chrome profile, harvest config, master lists, logs, results), bind-mounted at `/app/data`. Redis/Celery remain unused — harvests run as in-process asyncio background tasks.
- LinkedIn/Naukri login is **manual** by design — there's no credential automation. A human logs in once through the **live browser view** in the React UI (backed by the Xvfb/VNC stack above), and the session persists in a Chrome profile for all future harvest runs.

---

## 1. Run and test locally

Two ways to run it locally. Pick based on what you're testing:

| | Native (no Docker) | Full Docker Compose |
|---|---|---|
| Setup effort | Low | Higher (image build) |
| Speed | Fast iteration | Slower (rebuild on change) |
| Live browser view (noVNC) | ❌ Not available — Xvfb/x11vnc/websockify are Linux-only | ✅ Fully works |
| Matches production | Partially | Yes — same images that ship to EC2 |
| Use when | Iterating on backend/frontend logic | Testing deploy-specific behavior (login flow, nginx routing, live view) |

### 1a. Native (fast iteration, no Docker)

**Prerequisites:** Python 3.11+, Node 18+, and Playwright's Chromium browser installed (`python -m playwright install chromium` — one-time, ~300MB).

**Backend:**
```powershell
cd ai-harvest-agent
python run.py            # runs on http://127.0.0.1:8000
```
Use `run.py`, not `uvicorn app.main:app --reload` directly — on Windows, uvicorn's reload mode forces an event loop policy that breaks Playwright's ability to spawn browser subprocesses. `run.py` passes `loop="none"` specifically to avoid that.

Verify: `curl http://127.0.0.1:8000/health` → `{"status":"ok",...}`.

**Frontend:**
```powershell
cd harvest-agent
# one-time: point it at the native backend
"REACT_APP_API_BASE_URL=http://127.0.0.1:8000" | Out-File -Encoding utf8 .env.local
npm install
npm start                # http://localhost:3000
```

**What you can and can't test this way:** you can exercise all the harvest/config/job-listing endpoints. You will **not** see the live browser view — clicking "Connect" under LinkedIn/Naukri will open a real native Chromium window on your Windows desktop instead (headless=False, no Xvfb needed since Windows has a real display), and the React "LiveBrowserView" panel will just show "Disconnected" since nothing is listening on the VNC path. That's expected, not a bug — this mode is for logic iteration, not deploy verification.

**Cleanup when done:** stop both processes (Ctrl+C), and if you tested login/harvest, kill any leftover `chrome.exe` processes:
```powershell
Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force
```

### 1b. Full Docker Compose stack (matches production)

This is the one that actually exercises the live browser view and is the true "dress rehearsal" for EC2.

**Prerequisites:** Docker Desktop with the WSL2 backend enabled (Settings → General → "Use the WSL2 based engine"), or a native Docker Engine running inside a WSL2 distro.

```powershell
docker --version
docker compose version      # or: docker-compose --version, if using the older v1 binary
```

**Steps, from the repo root:**

```powershell
# 1. Env file
Copy-Item ai-harvest-agent\.env.example ai-harvest-agent\.env

# 2. Build (do this in stages the first time so failures are easy to isolate)
docker compose build api        # installs apt deps, Playwright Chromium, Python deps — several minutes first time
docker compose build frontend   # npm ci + react build + nginx

# 3. Bring it up
docker compose up -d
docker compose ps               # "postgres", "api" and "frontend" should show Up
```

Backend-only (no frontend/nginx, e.g. when iterating on the API against the
containerized stack): `docker compose up -d api` — postgres comes up
automatically via `depends_on`, and the api is published on
`localhost:8000` / noVNC on `localhost:6080` (`API_BIND_IP` overrides the bind).

**Verify layer by layer:**
```powershell
# supervisord's 4 programs should all be RUNNING
docker compose exec api supervisorctl status

# health through nginx (proves the whole proxy chain)
curl http://localhost/health

# React app itself
curl -o NUL -s -w "%{http_code}`n" http://localhost/
```

**Verify the live browser view (the actual feature this whole stack exists for):**
1. Open `http://localhost/` in a real browser.
2. Rule Engine → Connect Accounts → **Connect** under LinkedIn.
3. The `LiveBrowserView` panel should go "Connecting…" → "Connected — click in to type", showing the real LinkedIn login page rendered live.
4. Click into it and type — confirms keystrokes reach the remote browser.
5. `docker compose restart api`, then confirm `ai-harvest-agent/data/chrome_profile` on disk is populated and a later "Run Now" doesn't re-prompt login.

**Logs / debugging:**
```powershell
docker compose logs -f api
docker compose logs -f frontend
docker compose exec api ps aux   # look for a chromium process when live view is open
```

#### WSL2-specific quirks (harmless, but confusing if you don't know about them)

- **Idle VM shutdown:** WSL2's lightweight VM shuts down when idle. Since both containers are `restart: unless-stopped`, they auto-restart the next time you touch WSL2 — you may see a container with a suspiciously low uptime after stepping away. This is self-healing and is a WSL2-local behavior only; it does **not** happen on an always-on EC2 instance.
- **`docker-compose` v1 (the old Python binary, `docker-compose` not `docker compose`) has a known bug** recreating a container against a BuildKit-built image (`KeyError: 'ContainerConfig'`). If you hit it: `docker rm -f <container>` then `docker compose up -d` again (fresh create works fine; it's only the recreate/diff path that's broken).
- If pip/npm installs fail with DNS/network errors inside WSL2, check `curl -6 https://pypi.org` vs `curl -4 https://pypi.org` — broken IPv6 inside some WSL2 setups causes exactly this. Disable with `sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1` if so.

---

## 2. Deploy to AWS (EC2)

### Prerequisites
- AWS account with EC2 permissions, an SSH key pair.
- Real values for `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` (job parsing needs them).
- Code pushed to a Git remote the instance can pull from (or ready to `scp` it up).

### Step 1 — Launch the instance

| Setting | Value | Why |
|---|---|---|
| AMI | Ubuntu Server 22.04 LTS | matches the Debian/Ubuntu base images used here |
| Architecture | **64-bit (x86)** | images are built x86_64 — do not pick an ARM/Graviton (`t4g.*`) type unless you multi-arch build |
| Instance type | t3.large (2 vCPU/8GB); t3.medium viable cheaper fallback | Chromium+Xvfb+FastAPI is the only real memory pressure |
| Storage | 30GB gp3 | Docker images + `data/` (Chrome profile, results) |
| Security group | see below | |

**Security group:**

| Port | Source | Purpose |
|---|---|---|
| 22 | your IP only | SSH |
| 80 | 0.0.0.0/0 | app access (and ACME challenge if adding TLS) |
| 443 | 0.0.0.0/0 | only once TLS is added |

**Do not open port 6080 (websockify) publicly.** nginx already proxies `/vnc/` through 80/443 — a public 6080 would let anyone reach a live, logged-in browser session with zero auth.

(Optional: allocate an Elastic IP so the address survives instance stop/start.)

### Step 2 — Connect and install Docker

```bash
ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>

sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER
# log out and back in for the group change to take effect
```

### Step 3 — Get the code and configure

```bash
git clone https://github.com/<your-org>/jobharvestingss.git
cd jobharvestingss
cp ai-harvest-agent/.env.example ai-harvest-agent/.env
nano ai-harvest-agent/.env
# The data directory is ai-harvest-agent/data — the seed files (master lists,
# default config) ship in the repo; no mkdir needed. If this instance ran an
# older version that used a repo-root data/ dir, merge that dir's contents into
# ai-harvest-agent/data/ BEFORE the first `docker compose up` (it holds the
# saved LinkedIn session in chrome_profile/).
```

What matters in `.env` (`REDIS_URL` / `CELERY_*` are unused dead config, safe to leave as-is):
- `APP_ENV=production`, `APP_DEBUG=false`
- `DATABASE_URL` + `POSTGRES_DB/USER/PASSWORD` — the app's PostgreSQL database (see `POSTGRES_MIGRATION.md`)
- `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` — required for job parsing
- `CORS_ORIGINS` — comma-separated origins; harmless to leave the default since frontend+backend share one nginx origin
- `API_KEY` — currently only guards a couple of internal/unused endpoints; the routes the React app actually calls have no auth yet. Revisit before this holds real production accounts long-term.

### Step 4 — Build and start

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f api    # watch supervisord bring up xvfb/x11vnc/websockify/api cleanly
```

Then verify exactly like the local Docker pass, against the public IP instead of `localhost`:
1. `http://<EC2_PUBLIC_IP>/` loads, health badge green.
2. Connect Accounts → **Connect** under LinkedIn → live view renders the real login page.
3. Log in for real (this is the one-time setup).
4. "Run Now" completes without re-prompting login.
5. `docker compose restart api` — session in `ai-harvest-agent/data/chrome_profile` survives.

### Step 5 — TLS (optional, once you have a domain)

Two options:
- **Certbot on the host**, fronting the containers — point your domain's A record at the Elastic IP, get a cert (briefly stopping the `frontend` container to free port 80 for standalone mode), add an HTTPS server block to `harvest-agent/nginx.conf`, set up a renewal cron.
- **AWS ALB + ACM certificate** in front of the instance — no renewal to manage, small extra monthly cost.

Until either is done, the app is fully functional over plain HTTP — a legitimate state for an initial deployment.

### Ongoing operations

```bash
# Deploy an update
git pull && docker compose build && docker compose up -d

# Logs
docker compose logs -f api
docker compose logs -f frontend

# Backup the stateful data: the data dir (Chrome profile + config + results)
# and the PostgreSQL database
tar czf backup-$(date +%Y%m%d).tar.gz ai-harvest-agent/data/
docker compose exec -T postgres pg_dump -U harvest harvest_db | gzip > pgdump-$(date +%Y%m%d).sql.gz
```
Both services are `restart: unless-stopped`, and Docker itself is enabled as a systemd service by the install steps above, so a full instance reboot recovers on its own.

---

## Appendix: bugs found and fixed while validating this setup

These were real, reproducible issues caught by actually building and running the stack — not hypothetical:

1. **`python:3.11-slim` base image** now resolves to Debian trixie, whose renamed font packages (`ttf-unifont`, `ttf-ubuntu-font-family`) break `playwright install --with-deps`. Fixed by pinning `ai-harvest-agent/Dockerfile` to `python:3.11-slim-bookworm`.
2. **`supervisord.conf`** was missing the `[unix_http_server]`/`[supervisorctl]` sections needed for `supervisorctl status` to connect to the running daemon — added.
3. **`cors_origins: list[str]`** in `app/config.py` crash-looped the app under any real `.env` file — pydantic-settings attempts to JSON-decode list-typed env vars before any custom validator runs, so the documented comma-separated `CORS_ORIGINS` value could never actually load. Fixed by changing the field to a plain `str` with a `cors_origins_list` property, consumed in `app/main.py`.

If you ever re-scaffold this Docker setup from scratch, watch for these three specifically.
