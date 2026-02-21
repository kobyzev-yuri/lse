# Low-cost Google Cloud server for LSE (all services)

This document describes a **low-cost single-server setup** on Google Cloud that runs all LSE services: PostgreSQL (with pgvector), cron jobs (prices, news, trading cycle, RSI), and the Telegram bot.

---

## What runs where

| Component | Role |
|-----------|------|
| **PostgreSQL** | Database: `quotes`, `knowledge_base` (with optional embedding, outcome_json), `portfolio_state`, `trade_history`. Requires **pgvector** extension. |
| **Cron** | `update_prices_cron.py`, `fetch_news_cron.py`, `trading_cycle_cron.py`, `update_rsi_local.py`, `update_finviz_data.py` (see `setup_cron.sh`). |
| **Telegram bot** | Either **polling** (no public URL) or **webhook** (needs a small HTTP server, e.g. FastAPI on a fixed port). |

All of the above can run on **one small Compute Engine VM**. Optionally you can split: bot on **Cloud Run** and DB + cron on one VM (see “Alternative” below).

---

## Recommended: single VM (all-in-one)

### 1. Instance type and cost

Use a **single Compute Engine VM** in a cheap region (e.g. `europe-west1` or `us-central1`).

| Option | Machine type | vCPU | RAM | Approx. monthly cost (on-demand) |
|--------|--------------|------|-----|----------------------------------|
| **Minimal** | `e2-small` | 2 | 2 GB | ~\$15–20 |
| **Comfortable** | `e2-medium` | 2 | 4 GB | ~\$25–35 |

- **e2-small**: Enough for Postgres + cron + bot in **polling** mode. If you use **sentence-transformers** (vector KB) on the same box, 2 GB can be tight; prefer e2-medium.
- **e2-medium**: Safer for Postgres + cron + bot + embeddings; recommended if you use Vector KB or LLM locally.

**Disk:** Add a **30–50 GB** balanced persistent disk (standard or balanced). Roughly **+\$3–6/month**.

**Total rough estimate:** **~\$20–40/month** for one e2-small/e2-medium VM + disk.

### 2. Create the VM (gcloud)

```bash
export PROJECT_ID=your-gcp-project
export REGION=europe-west1
export ZONE=europe-west1-b
export VM_NAME=lse-server

gcloud compute instances create $VM_NAME \
  --project=$PROJECT_ID \
  --zone=$ZONE \
  --machine-type=e2-small \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-balanced
```

Allow HTTP/HTTPS if you will expose the bot webhook (optional):

```bash
gcloud compute firewall-rules create allow-http-8080 \
  --project=$PROJECT_ID \
  --allow=tcp:8080 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=lse-server
```

Tag the instance if you used a target tag:

```bash
gcloud compute instances add-tags $VM_NAME --zone=$ZONE --tags=lse-server
```

### 3. On the VM: install stack

SSH into the VM, then:

**3.1 PostgreSQL + pgvector**

```bash
sudo apt-get update
sudo apt-get install -y postgresql postgresql-contrib
# pgvector: follow https://github.com/pgvector/pgvector#installation
# or: sudo apt-get install postgresql-16-pgvector  # if available for your PG version
sudo -u postgres createuser -s lse
sudo -u postgres createdb -O lse lse_trading
sudo -u postgres psql -d lse_trading -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

**3.2 Python (Conda or system)**

```bash
# Option A: Miniconda
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p $HOME/miniconda3
$HOME/miniconda3/bin/conda create -n py11 python=3.11 -y
# then: source $HOME/miniconda3/bin/activate py11

# Option B: system Python 3.11
sudo apt-get install -y python3.11 python3.11-venv python3-pip
python3.11 -m venv /opt/lse/venv
source /opt/lse/venv/bin/activate
```

**3.3 Clone repo and install deps**

```bash
sudo mkdir -p /opt/lse && sudo chown $USER /opt/lse
cd /opt/lse
git clone https://github.com/YOUR_ORG/lse.git .
# use conda py11 or venv
pip install -r requirements.txt
```

**3.4 Config**

Create `/opt/lse/config.env` (or `.env`) with:

- `DATABASE_URL=postgresql://lse:YOUR_PASSWORD@localhost:5432/lse_trading`
- `TELEGRAM_BOT_TOKEN=...`
- `TELEGRAM_ALLOWED_USERS=...` (optional)
- Any API keys: `ALPHAVANTAGE_KEY`, `NEWSAPI_KEY`, etc.

Load it before running the app (e.g. `set -a && source /opt/lse/config.env && set +a`).

**3.5 Init DB and cron**

```bash
cd /opt/lse
python init_db.py   # create tables, seed if needed
./setup_cron.sh     # install crontab for prices, news, trading cycle, RSI
```

Cron will use the same Python (conda or venv) that you use when you run `./setup_cron.sh`; ensure `which python3` points to that environment.

**3.6 Run the Telegram bot**

- **Polling (simplest, no public URL):**  
  `cd /opt/lse && python scripts/run_telegram_bot.py`  
  Run under systemd or screen/tmux so it survives disconnects.

- **Webhook (for production):**  
  Run `api/bot_app.py` (FastAPI) on port 8080, set Telegram webhook to `https://YOUR_VM_EXTERNAL_IP:8080/webhook` (you’ll need HTTPS; e.g. reverse proxy with Let’s Encrypt, or a load balancer with SSL). Then cron and bot both use the same VM; Cloud Run is not required.

### 4. Summary: one VM

- **One e2-small (or e2-medium) VM** in a cheap region.
- **PostgreSQL + pgvector** on the same VM (localhost).
- **Cron** on the VM via `setup_cron.sh`.
- **Telegram bot** on the VM (polling or webhook).
- **Rough cost:** ~\$20–40/month (VM + disk, before any free-tier or sustained-use discounts).

---

## Alternative: Cloud Run (bot) + one VM (DB + cron)

If you prefer the bot to be on Cloud Run (scale-to-zero, managed HTTPS):

- **Cloud Run:** Run only the Telegram webhook service (e.g. `api/bot_app.py`). Set `min-instances=0` to reduce cost; you pay per request. Typically **a few \$/month** for low traffic.
- **One small VM:** Same as above: PostgreSQL (with pgvector) + cron. The VM needs a **static IP** or a **VPC** so Cloud Run can reach it (e.g. VPC connector + private IP, or Cloud SQL instead of Postgres on VM).
- **Cost:** VM (~\$20–35) + Cloud Run (low) + optional Cloud SQL if you replace self-hosted Postgres (~\$25–50 for smallest). Total roughly **~\$25–90/month** depending on DB choice.

This matches the existing “Cloud Run + separate server” design in `docs/DEPLOY_INSTRUCTIONS.md` and `BUSINESS_PROCESSES.md` (sections 10–11).

---

## Cost comparison (rough)

| Setup | Approx. monthly | Notes |
|-------|------------------|------|
| **Single VM (e2-small, all-in-one)** | ~\$20–25 | Postgres + cron + bot (polling). Simplest. |
| **Single VM (e2-medium, all-in-one)** | ~\$30–40 | More headroom for embeddings/LLM. |
| **Cloud Run + 1 VM (Postgres + cron)** | ~\$25–45 | Bot on Run; DB and cron on VM; need VPC/private IP or Cloud SQL. |
| **Cloud Run + Cloud SQL (small)** | ~\$35–60 | Managed DB; no VM for Postgres. |

All estimates are before free tier or committed use discounts; actual billing depends on region and usage.

---

## References

- **Deploy (split architecture):** [docs/DEPLOY_INSTRUCTIONS.md](DEPLOY_INSTRUCTIONS.md) — Cloud Run + separate DB server.
- **Cron and scripts:** [setup_cron.sh](../setup_cron.sh), [docs/CRON_TICKERS_EXPLANATION.md](CRON_TICKERS_EXPLANATION.md).
- **Business processes:** [BUSINESS_PROCESSES.md](../BUSINESS_PROCESSES.md) — sections 10 (Telegram bot), 11 (deployment).
