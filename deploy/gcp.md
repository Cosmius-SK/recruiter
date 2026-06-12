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

## Option B — Cloud Run (serverless; trusted HTTPS URL)

One service serves **both** the website and the API (the API mounts
`website/` at `/`). This is also the recommended path when a corporate
network blocks raw-IP / non-443 / plain-HTTP URLs: Cloud Run gives you a
`https://….run.app` URL with a valid Google-managed certificate on port 443.

```bash
# From a clone of the repo (e.g. in Cloud Shell):
gcloud run deploy talentflow --source . --region=us-east1 \
  --allow-unauthenticated \
  --min-instances=1 --max-instances=1 \
  --timeout=900 \
  --set-env-vars GOOGLE_API_KEY=YOUR_GEMINI_KEY
```

The command prints the service URL, e.g. `https://talentflow-xxxxx-ue.a.run.app`:

| URL | What |
|---|---|
| `https://<service-url>/` | Product website |
| `https://<service-url>/docs` | API explorer |

Notes:
- `--max-instances=1` keeps all workflow state on one instance (the demo uses
  SQLite); `--min-instances=1` stops scale-to-zero from discarding it between
  uses. For production, switch the checkpointer to **Cloud SQL Postgres**
  (`langgraph-checkpoint-postgres`, swap `SqliteSaver` for `PostgresSaver` in
  `talentflow/api/app.py`) — then instances can scale freely.
- Prefer Secret Manager over `--set-env-vars` once past testing:
  `--set-secrets=GOOGLE_API_KEY=gemini-api-key:latest` (grant the service
  account `roles/secretmanager.secretAccessor`).
- The first deploy enables Cloud Build and may take a few minutes.

---

## Where to keep the API key — summary

| Context | Recommended home |
|---|---|
| Your GCP VM / Cloud Run | **Secret Manager**, injected into `.env` / `--set-secrets` |
| Local development | `.env` file (gitignored, auto-loaded) |
| GitHub Actions CI | GitHub repository secrets → job env var |

Never commit a key; `.env` is gitignored and the app reads keys only from the
environment.
