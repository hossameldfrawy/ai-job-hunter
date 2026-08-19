# Deployment

Goal: the bot hunts 24/7 with **zero dependency on your PC being on**.

---

## Which option should you use?

| Option | Truly free 24/7? | Always-on | Setup | Verdict |
|---|:--:|:--:|---|---|
| **GitHub Actions** | **Yes** (public repo) | Scheduled, every 30 min | ~10 min | **Use this.** |
| Oracle Cloud "Always Free" VM | Yes | Continuous daemon | ~40 min | Best runner-up; a real always-on server |
| Any VPS / NAS / Raspberry Pi | If you own it | Continuous daemon | ~10 min | Great if you already have one |
| Railway | No — trial credit, then paid | Continuous | ~5 min | Easiest paid option |
| Render | No — workers/cron are paid plans | Continuous | ~5 min | Free tier web services sleep, which breaks the loop |
| Fly.io | Depends on current allowance | Continuous | ~15 min | Fine if it fits your allowance |
| n8n (self-hosted or cloud) | Depends on host | Scheduled | ~20 min | Choose it if you want a visual, editable flow |

Being blunt about the "free serverless" tiers: Render's free plan covers web
services that **spin down after ~15 minutes of inactivity**, which stops a hunt
loop dead; its Background Workers and Cron Jobs are paid. Railway gives trial
credit, then bills. GitHub Actions is the only option in that list that is
genuinely free and genuinely runs forever — which is why the repository is
built around it.

---

## Option 1 — GitHub Actions (recommended)

### 1. Create the repository

```bash
cd "C:\Users\hossa\OneDrive\Desktop\NEW SHAPTER\AI_Job_Hunter_Bot"
git init
git add -A
git commit -m "AI Job Hunter"
gh repo create ai-job-hunter --public --source=. --remote=origin --push
```

**Why public?** Public repositories get **unlimited free Actions minutes**;
private ones get 2,000/month, and a 30-minute cadence needs roughly 5,800.

Public is safe here *by construction*: `.gitignore` excludes `.env`,
`assets/master_cv.pdf`, `secrets/` and the state database. Your CV and every
key live in encrypted GitHub Secrets, never in the repo. Verify before pushing:

```bash
git status --porcelain --ignored | grep -E "\.env|master_cv|secrets/"
```

Prefer private anyway? Fine — change the cron to `0 * * * *` (hourly, ~1,450
minutes/month) and it fits the free allowance.

### 2. Set the secrets

```bash
python setup_wizard.py --secrets    # prints these, pre-filled
```

```bash
gh secret set GEMINI_API_KEY   --body "your-key"
gh secret set CALLMEBOT_APIKEY --body "your-apikey"
gh secret set WHATSAPP_PHONE   --body "+201234567890"   # <- YOUR number
gh secret set CV_TEXT          < secrets/CV_TEXT.txt
```

`CALLMEBOT_API_KEY` is accepted as an alias for `CALLMEBOT_APIKEY`, so either
spelling works and a typo cannot silently disable alerts.

| Secret | Required | Notes |
|---|:--:|---|
| `GEMINI_API_KEY` | yes | https://aistudio.google.com/apikey |
| `CALLMEBOT_APIKEY` | yes | From the CallMeBot activation reply |
| `WHATSAPP_PHONE` | yes | International format, leading `+` |
| `CV_TEXT` | yes* | Plain-text CV. **Preferred.** Generate with `python setup_wizard.py --extract-cv`; it refuses to write past 64 KB |
| `MASTER_CV_B64` | no | base64 PDF. Only if under GitHub's 64 KB secret cap — a 57 KB PDF becomes 76 KB and will **not** fit, so use `CV_TEXT` |
| `TELEGRAM_API_ID` | no | Unlocks your joined **private** Telegram groups |
| `TELEGRAM_API_HASH` | no | Pairs with the api_id |
| `TELEGRAM_STRING_SESSION` | no | Produced by `python auth_telegram.py`. **Full login to your account** — encrypted secret only |
| `FACEBOOK_COOKIE` | no | Opt-in Facebook page reading |

\* one of `CV_TEXT` or `MASTER_CV_B64`.

### 2b. Optional: unlock your private Telegram groups

Without this the bot reads public channels only. With it, it reads every group,
supergroup and restricted channel you have joined.

```bash
# once, on your own machine (needs the login code Telegram sends you)
python auth_telegram.py
python check_telegram.py          # confirm it sees your groups

# then push the session to the cloud
gh secret set TELEGRAM_API_ID          --body "<your api_id>"
gh secret set TELEGRAM_API_HASH        --body "<your api_hash>"
gh secret set TELEGRAM_STRING_SESSION  < secrets/TELEGRAM_STRING_SESSION.txt
```

The workflow prints a notice on every run saying whether the Telegram client is
ON or OFF, so you can tell at a glance from the Actions log.

Scheduled runs use **poll** mode: they sign in, read what is new since the last
cursor, and disconnect. The event-driven **live** listener needs a process that
stays alive and therefore belongs on Docker/VPS (`python main.py --live`), not
on Actions — a scheduled runner is destroyed the moment the job finishes.

If the session ever stops working (you revoked it, or Telegram expired it), the
run logs `The Telegram session is invalid, expired or was revoked` and carries
on with the other sources. Re-run `python auth_telegram.py` and update the
secret.

### 2c. Runtime and cadence

A full sweep takes about **6 minutes** when Gemini is healthy, and up to
**11 minutes** when it is rate-limiting — measured, not estimated. Scraping is
~5 min of that (LinkedIn dominates: 92 paged requests behind a 2.5 s per-host
throttle, with Tanqeeb, talent.com and the Telegram client running concurrently
underneath). Evaluation is ~1 min normally but stretches to ~6 min when the free
tier throttles and batches have to back off. Both fit inside the 25-minute job
timeout with room to spare.

The schedule ships at `*/30`. To go to `*/15`, edit the cron in the workflow --
but note Gemini's free tier is metered per day as well as per minute. Steady
state is roughly 100 calls/day at `*/30` and 200 at `*/15`; the first day also
burns through the initial backlog. Start at `*/30`, watch `python main.py
--stats`, and tighten once it looks calm.

To cut runtime instead, trim `linkedin.queries[].locations` or lower
`linkedin.enrich_budget` in `config.yml` -- those are the two dials that matter.

### 3. Start it

```bash
gh workflow run job_hunter.yml                      # run immediately
gh run watch                                        # follow it live
gh workflow run job_hunter.yml -f dry_run=true      # test with no messages
```

From then on it runs every 30 minutes on its own.

### 4. Confirm it is healthy

- **Actions tab → the run → Summary.** Every run publishes a table: postings
  per source, the full funnel, Gemini token usage, and the best matches found.
- **`bot-state` branch.** It should exist and update every run. That is your
  deduplication database.
- **Your phone.** Anything scoring ≥ 75% arrives within a couple of minutes.

### How state survives a disposable runner

Each run gets a fresh VM that is destroyed afterwards, so the dedup database
cannot live on disk. It is force-pushed as a **single commit** to an orphan
branch named `bot-state`:

- the database persists across thousands of runs;
- repository size stays constant — no accumulating binary history;
- `main` is never touched;
- the regular pushes count as repo activity, so GitHub does not auto-disable
  the schedule after 60 idle days.

Delete that branch and the bot forgets everything, then re-alerts you about
jobs you have already seen. Leave it alone.

### Notes on the schedule

GitHub queues scheduled workflows under load, so `*/30` means "at least every
30 minutes", not a real-time guarantee. Delays of 5–15 minutes at peak are
normal and harmless here. `concurrency: ai-job-hunter` guarantees two runs
never overlap, which would otherwise fork the database and cause duplicates.

---

## Option 2 — Oracle Cloud Always Free VM (best always-on free option)

Oracle's Always Free tier includes ARM VMs that genuinely run forever.

```bash
# On a fresh Ubuntu VM
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER && newgrp docker

git clone https://github.com/<you>/ai-job-hunter.git && cd ai-job-hunter
cp .env.example .env && nano .env        # paste your keys
docker compose up -d
docker compose logs -f
```

`restart: unless-stopped` brings it back after any reboot. State lives in the
`hunter-state` Docker volume. Health: `curl localhost:8080`.

**This is the deployment that unlocks real-time Telegram.** Change the compose
command to `python main.py --live` and the container holds an open MTProto
connection: a job posted in one of your groups is scored and can be on WhatsApp
within seconds, while the periodic sweep keeps covering LinkedIn, talent.com,
the job APIs and RSS underneath it.

The same steps work on any VPS, a NAS, or a Raspberry Pi.

---

## Option 3 — Railway

```bash
railway login && railway init && railway up
railway variables set GEMINI_API_KEY=... CALLMEBOT_APIKEY=... WHATSAPP_PHONE=+201234567890
railway variables set CV_TEXT="$(cat secrets/CV_TEXT.txt)"
```

`railway.json` already sets the Dockerfile builder, `python main.py --daemon`,
a health check and a restart policy. **Attach a volume at `/app/state`** or the
dedup database resets on every redeploy and you get repeat alerts.

## Option 4 — Render

`render.yaml` defines a worker (Option A) and a cron job (Option B). Enable one.
Both require a paid plan — see the note at the top of that file. The disk mount
at `/var/data` plus `DB_PATH=/var/data/jobs.db` is what persists state.

## Option 5 — Fly.io

```bash
fly launch --no-deploy
fly secrets set GEMINI_API_KEY=... CALLMEBOT_APIKEY=... WHATSAPP_PHONE=+201234567890
fly secrets set CV_TEXT="$(cat secrets/CV_TEXT.txt)"
fly volumes create hunter_state --size 1
fly deploy
```

`auto_stop_machines = false` is deliberate — the hunt loop must keep ticking.

---

## Option 6 — n8n

Import `n8n_workflow.json` (Workflows → Import from File). Fourteen nodes:

```
Schedule (30m) ─┐
                ├─▶ Configuration ─▶ Fetch HTML ─▶ Parse ─▶ Dedupe+Prefilter
Manual Trigger ─┘                                              │
                                                               ▼
   WhatsApp ◀── Throttle 8s ◀── Split 1-at-a-time ◀── Any Matches? ◀── Gemini
```

Then edit **only** the `Configuration` node: paste your keys and CV text, or
set `GEMINI_API_KEY`, `CALLMEBOT_APIKEY`, `WHATSAPP_PHONE` and `CV_TEXT` as
environment variables on the n8n host (the node reads `$env` first).

Deduplication uses n8n's persistent workflow static data, capped at the last
8,000 fingerprints, so it survives restarts the same way the SQLite build does.

Differences from the Python build, so you can choose deliberately:

| | Python | n8n |
|---|---|---|
| Sources | 7 (LinkedIn, Telegram, talent.com, 6 APIs, RSS, search proxy, Facebook) | 2 (LinkedIn, Telegram) |
| Dedup | SQLite, content + URL keys | static data, content key |
| Editing | code | visual |

The n8n flow is the same architecture, deliberately trimmed to the two
highest-signal sources so the graph stays readable.

---

## Operating it

```bash
python main.py --stats     # lifetime funnel and recent runs
python main.py --prune     # compact the database (safe any time)
```

To pull the cloud database down and inspect it locally:

```bash
git fetch origin bot-state
git show origin/bot-state:jobs.db > state/jobs.db
python main.py --stats
```

### Emergency controls

Repository **Variables** (not secrets) act as live switches without a redeploy:

| Variable | Effect |
|---|---|
| `MATCH_THRESHOLD=85` | Fewer, stricter alerts |
| `MAX_ALERTS_PER_RUN=5` | Cap the volume |
| `DISABLE_LINKEDIN=1` | Turn one source off (`DISABLE_TELEGRAM`, `DISABLE_TALENT`, ...) |
| `DRY_RUN=true` | Keep hunting, stop sending |
| `DISABLE_TANQEEB=1` | Turn off the Arabic/GCC aggregator |

To get a one-off proof that every platform is reachable from the cloud runner
(not just from your machine), trigger the workflow and read the run summary, or
run `python main.py --digest` locally — the source list and per-source samples
are identical because both use the same scrapers.

To pause entirely: **Actions → AI Job Hunter → ⋯ → Disable workflow.**

---

## Cloud troubleshooting

**Workflow does not run on schedule.** GitHub disables schedules on repos with
60 days of no activity — the `bot-state` push normally prevents this. Check
Actions → the workflow is not disabled. Also confirm the file is on your
**default branch**; scheduled workflows only run from there.

**"Missing repository secrets".** The pre-flight step names exactly which one.
Re-run `python setup_wizard.py --secrets`.

**Alerts repeat.** The `bot-state` branch is missing or was deleted, so state
resets each run. Confirm the "Persist state" step is succeeding and that the
workflow has `permissions: contents: write`.

**Run fails with HTTP 429 from Gemini.** Free-tier rate limiting. Lower
`engine.eval_concurrency` to 1 and raise `eval_batch_size` to 10–12.

**Telegram says "session is invalid, expired or was revoked".** Someone
terminated the session in Telegram → Settings → Devices, or it aged out. Re-run
`python auth_telegram.py` and update the `TELEGRAM_STRING_SESSION` secret.

**Telegram logs a flood-wait.** Telegram is asking for a slower pace and the
client is obeying it — that is the designed behaviour, not a fault. Reduce
`telegram_user.messages_per_dialog` or `max_dialogs` in `config.yml` if it
recurs every run.

**A source reports 0 postings.** Normal for one source occasionally — the
circuit breaker skips a host after repeated failures and retries next run. If
`talent` logs "ALL countries returned zero cards", its CSS selectors need
updating (`SEL_*` in `scrapers/talent.py`); the site changed its markup.
