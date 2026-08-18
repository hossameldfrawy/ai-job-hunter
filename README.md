# AI Job Hunter

An autonomous job-hunting bot. It watches LinkedIn, Telegram, GCC job boards
and a set of free job APIs around the clock, scores every posting against your
CV with Gemini, and sends you a WhatsApp message the moment something scores
75% or higher.

It runs entirely on GitHub's infrastructure. **Your computer can be off.**

```
🚨 NEW HIGH-MATCH JOB FOUND (Score: 88%)
🏢 Company: Etisalat
📍 Location: Dubai, United Arab Emirates
💼 Role: VoIP Engineer
🔗 Link: https://ae.linkedin.com/jobs/view/...
📡 Source: linkedin
✅ Why You Match: Requires Asterisk/Issabel PBX administration and SIP trunk
   troubleshooting, which maps directly to your VoIP support work.
⚠️ Gaps to address: Cisco CCNA Voice certification
```

---

## How it works

```
  SOURCES                    FILTERING                   AI + DELIVERY
  ─────────                  ─────────                   ─────────────
  LinkedIn guest API  ─┐
  Telegram channels    │     age gate                    Gemini scores
  talent.com (GCC)     ├──▶  deduplication  ──▶  ~7%  ─▶ each posting  ─┐
  6 free job APIs      │     lexical pre-filter          against your   │
  Google News proxy    │                                 CV (0-100)     │
  RSS feeds           ─┘                                                │
                                                                        ▼
                                                          score ≥ 75 → WhatsApp
```

A representative live run: **1,730 postings scraped → 120 evaluated by AI →
5 alerts sent.** The funnel exists because Gemini's free tier is metered; the
cheap filters do the bulk elimination so the expensive stage only ever sees
postings that are both new and plausible.

### Sources, and how honest each one is

| Source | Method | Credentials | Status |
|---|---|---|---|
| **LinkedIn** | Public logged-out guest endpoint | none | Best source. Fresh, structured, includes full descriptions |
| **Telegram** | `t.me/s/<channel>` web preview | none | Works for any public channel. Arabic posts handled |
| **talent.com** | Regional HTML (ae/sa/qa/kw/om/bh/eg) | none | Most productive GCC-native board that serves bots |
| **Job APIs** | arbeitnow, remoteok, jobicy, himalayas, remotive, themuse | none | Clean JSON, skews remote/global |
| **Bayt, GulfTalent, Naukrigulf, Wuzzuf** | Google News RSS proxy | none | These boards return **HTTP 403** to bots. The proxy is the only free path; links are Google redirects |
| **RSS** | Any feed you add | none | Low maintenance |
| **Facebook** | Indexed posts, or your own cookie | optional | Facebook blocks anonymous scraping entirely. Low volume without a cookie |

Anything claiming to scrape Bayt or Indeed directly for free is not telling you
the truth — both hard-block datacentre IPs. This project routes around that and
says so rather than failing silently.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in your keys
python setup_wizard.py        # verifies everything + sends a test WhatsApp
python main.py --dry-run      # full pipeline, sends nothing
python main.py                # the real thing
```

`setup_wizard.py` checks the runtime, dependencies, credentials, CV extraction,
Gemini connectivity (with a real scoring round-trip), WhatsApp delivery, the
database and every enabled source — and stops at the first genuine failure so
you fix one problem, not a cascade.

### Deploy it to the cloud

See **[DEPLOYMENT.md](DEPLOYMENT.md)**. The short version:

```bash
python setup_wizard.py --secrets   # prints the exact `gh secret set` commands
git push -u origin main
gh workflow run job_hunter.yml
```

---

## Configuration

Two layers, and the split matters:

- **`.env` / GitHub Secrets** — credentials and your CV. Never committed.
- **`config.yml`** — every tuning knob. Safe to commit and edit freely.

The knobs you are most likely to touch:

```yaml
engine:
  match_threshold: 75        # alert at/above this score
  max_alerts_per_run: 12     # storm protection
  max_job_age_days: 21       # ignore stale postings

profile:
  primary_keywords:   [voip, sip, issabel, asterisk, ...]   # worth 3 points
  secondary_keywords: [it support, odoo, python, ...]       # worth 1 point
  negative_keywords:  [nurse, driver, accountant, ...]      # instant reject
  target_locations:   [dubai, riyadh, doha, cairo, remote, ...]
```

**Getting too many alerts?** Raise `match_threshold` to 80–85.
**Getting too few?** Lower it to 70, and add keywords to `secondary_keywords`.

### Adding Telegram channels

Telegram publishes no channel-search API, so channels can only be added by
exact `@username`. Validate before adding — most job channels people recommend
turn out to be dormant:

```bash
python discover_channels.py @somechannel @another --add
```

```
  CHANNEL             VERDICT       LAST  IT   OFF  SUBS
  telecomcareers      RECOMMENDED   5d    67   0    9.9K subscribers
  gulfjobsin          DORMANT       59d   65   0    5.08K subscribers
  jobsgulf            WRONG NICHE   1528d 2    30   1.51K subscribers
```

The six channels shipped in `config.yml` were selected this way from ~70
candidates; the rest were dead, dormant, or posting medical jobs.

---

## Commands

| Command | What it does |
|---|---|
| `python main.py` | One hunt, then exit (what the cloud runs) |
| `python main.py --dry-run` | Everything except sending WhatsApp messages |
| `python main.py --daemon` | Run forever on an interval (Docker/VPS) |
| `python main.py --stats` | Lifetime statistics and recent run history |
| `python main.py --selftest` | Verify Gemini + WhatsApp connectivity |
| `python main.py --prune` | Compact the deduplication database |
| `python setup_wizard.py` | Full setup verification |
| `python discover_channels.py` | Audit Telegram channels |
| `python tests/test_pipeline.py` | Offline test suite (41 tests, no network) |

---

## Project layout

```
main.py                 CLI entry point and run modes
pipeline.py             Orchestration: ingest → filter → evaluate → alert
config.py               Settings (env secrets + config.yml)
models.py               JobPost / Evaluation, fingerprinting, URL canonicalisation
db.py                   SQLite dedup store, alert ledger, run history
relevance.py            Cheap lexical pre-filter (the Gemini quota guard)
evaluator.py            Gemini engine: batching, strict JSON schema, model fallback
notifier.py             CallMeBot WhatsApp dispatch
cv_profile.py           CV loading from secret, base64 PDF, or file
http_client.py          Throttling, retries, UA rotation, circuit breaker
scrapers/               One module per source
  ├─ linkedin.py        Public guest endpoint + description enrichment
  ├─ telegram_web.py    t.me/s/ preview (no credentials)
  ├─ telegram_api.py    Telethon, for private channels (optional)
  ├─ talent.py          talent.com regional boards
  ├─ job_apis.py        Six free JSON APIs
  ├─ search_proxy.py    Google News RSS for 403-blocked boards
  ├─ rss_feeds.py       Generic RSS/Atom
  └─ facebook.py        Indexed posts / optional cookie
setup_wizard.py         Setup, verification, secret export
discover_channels.py    Telegram channel auditor
n8n_workflow.json       Importable n8n visual workflow
.github/workflows/      The 24/7 GitHub Actions engine
```

---

## Design decisions worth knowing

**Deduplication is content-based, not URL-based.** The same role syndicated to
LinkedIn, talent.com and a Telegram channel produces one alert, not three. The
fingerprint normalises company + title + location, strips recruiter filler
("URGENT Hiring:"), and folds location granularity, so `Dubai, Dubai, UAE` and
`Dubai, UAE` are the same place. URL hashing runs as a second, independent key.

**State lives on a git branch.** GitHub Actions runners are destroyed after
every run, so the SQLite database is force-pushed as a single commit to an
orphan branch called `bot-state`. The database survives forever, repository
size stays constant, and `main` is never touched. A side effect: the push
counts as repository activity, which stops GitHub auto-disabling the schedule
after 60 idle days.

**Gemini is never trusted with links.** `direct_link` and `source_platform` are
copied from the scraped record, not from the model's output. A hallucinated
score is a bad recommendation; a hallucinated link is a dead end.

**The AI decides, the regex only pre-screens.** The lexical filter is
deliberately generous — it discards the obvious no (nursing, construction,
C-suite) and lets everything ambiguous through. `sip` uses word boundaries so
it does not match `gossip`; Arabic is preserved through normalisation because a
large share of Gulf postings are written in it.

**Failure is loud.** If every source returns zero, or Gemini fails on every
batch, you get a WhatsApp message saying so (rate-limited to one per three
hours). A bot that silently stops finding jobs is worse than no bot.

---

## Costs

| Component | Cost |
|---|---|
| GitHub Actions (public repo) | Free, unlimited minutes |
| Gemini API | Free tier is sufficient — batching keeps it to ~15 calls/run |
| CallMeBot | Free |
| **Total** | **Nothing** |

On a **private** repo you get 2,000 Actions minutes/month, and a 30-minute
cadence needs more than that. Either make the repo public (your secrets stay
encrypted, and nothing personal is committed — that is why the CV is a secret
rather than a file) or move the schedule to hourly. See DEPLOYMENT.md.

---

## Troubleshooting

**No WhatsApp messages arrive.** Confirm CallMeBot activation: send
`I allow callmebot to send me messages` to **+34 644 51 95 23** on WhatsApp and
use the API key it replies with. Then `python main.py --selftest`.

**No jobs are matching.** Run `python main.py --dry-run` and read
`run_report.json` — it counts exactly where postings were lost at each stage. A
high `prefilter_dropped` means your keywords are too narrow; a high `matched`
with zero alerts means everything was already sent.

**"Every ingestion source returned zero postings."** Usually a network block.
Check the per-source table in the run report or the Actions step summary.

**A scraper silently returns nothing.** Sites change their markup. `talent.py`
logs an explicit error when *every* country returns zero cards, which is the
signal that its CSS selectors need updating.

---

## Legal note

This reads publicly accessible pages — the same content a logged-out browser
sees — for personal job hunting, with conservative rate limiting on every
request. It does not bypass authentication or paywalls. The optional Facebook
and private-Telegram paths use *your own* credentials and are off by default.
Review each site's terms and decide for yourself what you are comfortable with.
