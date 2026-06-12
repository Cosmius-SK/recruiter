# Deploying TalentFlow on GCP

Two supported paths. **Option A (Compute Engine VM)** is the reference setup —
one VM runs nginx (product website + reverse proxy) and the API container.
**Option B (Cloud Run)** is the serverless alternative.

In both cases the LLM key is the only secret. Recommended source during the
testing phase: a **Gemini API key** (free tier) from
<https://aistudio.google.com/apikey>.

---

## Option A — your Compute Engine VM (reference)

### 1. One-time VM prep

```bash
# On the VM (Debian/Ubuntu):
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker
```

If the VM doesn't allow HTTP yet, open port 80 (from your workstation / Cloud Shell):

```bash
gcloud compute firewall-rules create allow-talentflow-http \
  --allow=tcp:80 --target-tags=http-server --direction=INGRESS
gcloud compute instances add-tags YOUR_VM_NAME --tags=http-server --zone=YOUR_ZONE
```

### 2. Get the code and the key onto the VM

```bash
git clone https://github.com/Cosmius-SK/recruiter.git && cd recruiter
cp .env.example .env
```

Put the key in `.env` — preferably pulled from **Secret Manager** rather than
typed in plaintext:

```bash
# One-time: store the secret (from any gcloud-authenticated shell)
echo -n "AIza..." | gcloud secrets create gemini-api-key --data-file=-

# On the VM (service account needs roles/secretmanager.secretAccessor):
echo "GOOGLE_API_KEY=$(gcloud secrets versions access latest --secret=gemini-api-key)" >> .env
```

### 3. Launch

```bash
docker compose up -d --build
```

That's the whole deployment:

| URL | What |
|---|---|
| `http://<vm-ip>/` | Product website |
| `http://<vm-ip>/api/docs` | API explorer (OpenAPI) |
| `POST http://<vm-ip>/api/workflows` | Start a lifecycle |

Workflow state persists in the `talentflow-data` volume — containers are
disposable, parked workflows survive restarts and redeploys.

### 4. Updating

```bash
git pull && docker compose up -d --build
```

### 5. HTTPS (when you attach a domain)

Point an A record at the VM, then either:
- **Certbot on the VM** — add a TLS server block to `deploy/nginx.conf` with
  `certbot certonly --webroot`, or
- **GCP HTTPS Load Balancer** with a Google-managed certificate in front of
  the VM (instance group backend) — no changes on the VM.

---

## Option B — Cloud Run (serverless)

```bash
# API — build & deploy from source, key injected from Secret Manager
gcloud run deploy talentflow-api --source . --region=YOUR_REGION \
  --set-secrets=GOOGLE_API_KEY=gemini-api-key:latest \
  --allow-unauthenticated

# Website — static hosting from a bucket (or a second nginx Cloud Run service)
gsutil mb -l YOUR_REGION gs://your-talentflow-site
gsutil -m rsync -r website gs://your-talentflow-site
gsutil web set -m index.html gs://your-talentflow-site
```

**Caveat:** Cloud Run filesystems are ephemeral, so the SQLite checkpointer
won't persist parked workflows across instances. For Cloud Run, switch the
checkpointer to **Cloud SQL Postgres** (`langgraph-checkpoint-postgres`,
swap `SqliteSaver` for `PostgresSaver` in `talentflow/api/app.py`). The VM
path needs no such change.

---

## Where to keep the API key — summary

| Context | Recommended home |
|---|---|
| Your GCP VM / Cloud Run | **Secret Manager**, injected into `.env` / `--set-secrets` |
| Local development | `.env` file (gitignored, auto-loaded) |
| GitHub Actions CI | GitHub repository secrets → job env var |

Never commit a key; `.env` is gitignored and the app reads keys only from the
environment.
