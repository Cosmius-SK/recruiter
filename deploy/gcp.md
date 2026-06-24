# Deploying Hire

**Recommended path: the free-tier Compute Engine VM behind your existing nginx
(Option A).** It has *no idle billing* and the app's background-thread design
works as-is. Cloud Run (Option B) is possible but, for this app's design, bills
a vCPU 24/7 — **read the billing-safety section first.**

The LLM key is the only secret. During testing use a **Gemini API key** (free
tier) from <https://aistudio.google.com/apikey>.

---

## 0. Billing safety — read this first

**What caused the June bill.** This app runs each workflow in a background
thread *after* the HTTP response returns and keeps run state warm in memory +
SQLite on the instance. On Cloud Run that requires `--no-cpu-throttling
--min-instances=1`, i.e. **one vCPU billed 24/7 even at zero traffic** — roughly
**₹1,200 / ~$14 a month** of pure idle cost. That is the entire Cloud Run charge
on the bill, not the VM (₹0.24) or Gemini (₹18.50).

**Prevent it from ever recurring:**

1. **Set a budget alert** (single most important control):
   Billing → *Budgets & alerts* → create a budget (e.g. **₹200/mo**) with email
   alerts at 50% / 90% / 100%. Budgets *alert*, they don't auto-cap — but a low
   threshold means you hear about a leak in days, not at the next invoice.
2. **Prefer the free VM (Option A).** No always-on instance, no idle billing.
3. **Default Cloud Run services to scale-to-zero** (`--min-instances=0
   --cpu-throttling`). Only override with a written reason + expected monthly cost.
4. **Sweep the classic orphans** periodically: release unused **static IPs**,
   delete **unattached disks**, and make boot disks **Standard PD** (the default
   *Balanced SSD* is not free-tier covered).
5. **30-second weekly check:** Billing → *Reports* → *Group by: SKU*. Anything
   new shows up immediately.

**Stop the current Cloud Run leak now** (run in Cloud Shell or any
gcloud-authenticated shell):

```bash
# Simplest: delete the service entirely (you're moving to the VM).
gcloud run services delete talentflow --region=us-east1

# Or keep it but stop idle billing (note: with this app's background-thread
# design, runs won't complete on a throttled/scale-to-zero service — see Option B):
gcloud run services update talentflow --region=us-east1 \
  --min-instances=0 --cpu-throttling
```

---

## Option A — free-tier VM behind your existing nginx (recommended)

Your VM already serves another app on 443. The Hire stack publishes only to the
host's **localhost:8080**, so **443 and the old app are untouched**; your host
nginx adds one `server` block that proxies a Hire subdomain → `127.0.0.1:8080`.

### 1. One-time VM prep

```bash
# On the VM (Debian/Ubuntu):
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker

# e2-micro has only 1 GB RAM. Add 2 GB swap so the Python container can't OOM
# the box (and take the old app down with it):
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 2. Code + key

```bash
git clone https://github.com/Cosmius-SK/recruiter.git && cd recruiter
git checkout claude/stoic-johnson-670j81      # current branch
cp .env.example .env
# Pull the key from Secret Manager rather than typing it in plaintext:
echo "GOOGLE_API_KEY=$(gcloud secrets versions access latest --secret=gemini-api-key)" >> .env
```

### 3. Launch (binds 127.0.0.1:8080 only)

```bash
docker compose up -d --build
```

State persists in the `talentflow-data` Docker volume — containers are
disposable, parked workflows survive restarts and redeploys. Verify locally on
the VM before exposing it:

```bash
curl -s localhost:8080/ | head -n 5            # website
curl -s localhost:8080/api/docs | head -n 5    # API explorer
```

### 4. Expose it on 443 via your host nginx + a trusted cert

Corporate networks blocked the raw IP / plain-HTTP URLs earlier — the fix is a
**real domain + Let's Encrypt cert**, exactly what made the `.run.app` URL work.

1. Point an **A record** for a subdomain (e.g. `hire.yourdomain.com`) at the
   VM's external IP. If the old app already has a domain on this VM, just add a
   `hire.` subdomain.
2. Drop the template `server` block into your host nginx and enable it:

   ```bash
   sudo cp deploy/nginx-host-hire.conf.example /etc/nginx/sites-available/hire
   # edit the server_name to your subdomain, then:
   sudo ln -s /etc/nginx/sites-available/hire /etc/nginx/sites-enabled/hire
   sudo nginx -t && sudo systemctl reload nginx
   ```
3. Issue the cert (certbot rewrites the block to add the 443 server + redirect):

   ```bash
   sudo certbot --nginx -d hire.yourdomain.com
   ```

| URL | What |
|---|---|
| `https://hire.yourdomain.com/` | Product website |
| `https://hire.yourdomain.com/app/` | **Hire Console** (the product UI) |
| `https://hire.yourdomain.com/api/docs` | API explorer |

### 5. Updating

```bash
git pull && docker compose up -d --build
```

---

## Option B — Cloud Run (serverless) — ⚠️ not the cheap path for *this* app

One service serves both the website and the API. A `.run.app` URL gives you a
trusted Google cert on 443 with no domain of your own — convenient, **but** this
app's background-thread design needs an always-on vCPU, so a *working* Cloud Run
deployment costs ~₹1,200/mo at idle (the June leak). A scale-to-zero deployment
is free at idle but **won't complete background runs** until the app is
re-architected to run synchronously to each human gate inside the request.

```bash
# Scale-to-zero (cheap, but background runs won't finish — see note above):
gcloud run deploy talentflow --source . --region=us-east1 \
  --allow-unauthenticated --min-instances=0 --cpu-throttling \
  --timeout=900 --set-env-vars GOOGLE_API_KEY=YOUR_GEMINI_KEY
```

To make Cloud Run both cheap *and* working, the change is: replace the
background thread in `talentflow/api/app.py` with a synchronous `invoke()` that
returns at each `interrupt()`, and move the checkpointer off `/tmp` SQLite to a
durable store (Cloud SQL Postgres via `langgraph-checkpoint-postgres`, or
Firestore). Ask if you want this built — then Cloud Run idle cost is ~₹0.

---

## Where to keep the API key — summary

| Context | Recommended home |
|---|---|
| Your GCP VM / Cloud Run | **Secret Manager**, injected into `.env` / `--set-secrets` |
| Local development | `.env` file (gitignored, auto-loaded) |
| GitHub Actions CI | GitHub repository secrets → job env var |

Never commit a key; `.env` is gitignored and the app reads keys only from the
environment.
