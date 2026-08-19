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
  LinkedIn (7 GCC)    ─┐
  Telegram public      │
  Telegram YOUR GROUPS │     age gate                    Gemini scores
  Tanqeeb (AR + EN)    ├──▶  deduplication  ──▶  ~7%  ─▶ each posting  ─┐
  talent.com (GCC)     │     bilingual                   against your   │
  6 free job APIs      │     lexical pre-filter          CV (0-100)     │
  Google News proxy    │                                                │
  RSS feeds           ─┘                                                ▼
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
| **Telegram (public)** | `t.me/s/<channel>` web preview | none | Works for any public channel. Arabic posts handled |
| **Telegram (your groups)** | MTProto user client (Telethon) | api_id + session | Reads the **private groups, supergroups and restricted channels you have joined** -- the web preview cannot see any of these |
| **Tanqeeb** | Regional HTML, 7 Arab subdomains | none | **Searches natively in Arabic.** The largest Arab-world aggregator that still serves bots; detail pages expose full `JobPosting` JSON-LD |
| **talent.com** | Regional HTML (ae/sa/qa/kw/om/bh/eg) | none | Second-most productive GCC-native board |
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

### Master CV resolution

The CV is resolved through a fallback chain, so a renamed or missing file
degrades instead of failing the run:

```
CV_TEXT secret  ->  MASTER_CV_B64 secret  ->  CV_PATH env  ->  cv.paths chain  ->  cache
```

`cv.paths` in `config.yml` lists the local candidates in order — the underscored
filename first, the spaced one as fallback, then the repo-local copy. Each can
fail three ways (missing, unreadable, or a scanned PDF with no extractable
text); all three fall through to the next candidate.

```bash
python setup_wizard.py --extract-cv    # writes secrets/CV_TEXT.txt
```

The exporter refuses to write past **64 KB**, because that is GitHub's
per-secret limit and `gh secret set` would reject it far less clearly. Your CV
exports to ~5 KB, about 8% of the budget. (The base64-PDF route is offered only
when it fits — for this CV it is 84 KB, so `CV_TEXT` is the supported path.)

### Bilingual matching (Arabic + English)

A large share of Gulf and Egyptian postings are written in Arabic, and naive
string matching misses almost all of them. Three things make the Arabic side
actually work:

**Orthographic folding.** The same word is spelled several ways in real
postings. `normalise_text` folds them into one form before anything is compared:

| Written as | Folds to | Why |
|---|---|---|
| `أخصائي` `اخصائي` `إخصائي` | `اخصايي` | alef variants (NFKD) |
| `تقنية` | `تقنيه` | teh marbuta → heh |
| `فنى` | `فني` | alef maksura → yeh |
| `مُهَنْدِس` | `مهندس` | harakat stripped |
| `ســـنترالات` | `سنترالات` | tatweel removed |

This also protects deduplication: two boards spelling the same Arabic role
differently produce **one** fingerprint, so you get one alert, not two.

**Script-aware word boundaries.** `` is defined against `\w`, which includes
Arabic, and behaves inconsistently once a line mixes scripts, punctuation and
RTL marks. Instead each edge of a term asserts against the script of its own
adjacent character — so `sip` cannot match inside `gossip`, `شبكات` cannot match
inside `الشبكاتيون`, and `3cx` still matches because a symbol edge gets no guard.

**Terms are normalised before compiling.** This one is easy to get wrong and
impossible to notice: the haystack is normalised, so a pattern built from the
raw term `تقنية معلومات` (with ة) could never match text that had already become
`تقنيه`. English terms are unaffected, which is exactly why the bug hides. There
is a regression test for it.

Arabic keywords live alongside the English ones in `config.yml` under
`profile:`, and Tanqeeb is queried in both languages.

### Watching your own Telegram groups

The public-channel scraper only sees channels that expose a `t.me/s/` web
preview. Most real recruitment communities are **private groups or supergroups**,
which have no preview at all. To read those, the bot signs in as you over
Telegram's native MTProto protocol:

```bash
python auth_telegram.py     # one-time, interactive: enter the login code
python check_telegram.py    # lists your groups + shows what the filter catches
```

`auth_telegram.py` produces two things: a local `.session` file for this
machine, and a portable `TELEGRAM_STRING_SESSION` string that lets GitHub
Actions sign in with no human present, forever.

> The string session is a **full login to your Telegram account**. It lives in
> `.env` (git-ignored) and in encrypted GitHub Secrets — never in the repo.
> Revoke it any time from Telegram → Settings → Devices → "AI Job Hunter".

Two modes:

| | What it does | Where it runs |
|---|---|---|
| **poll** (default) | Walks your dialogs each run, reads messages newer than a per-chat cursor | Anywhere, including GitHub Actions |
| **live** | Reacts to `events.NewMessage` the instant a post lands, so an alert can reach WhatsApp within seconds | Needs a persistent process: `python main.py --live` on Docker/VPS |

Scheduled Actions runs cannot hold an open connection — the runner is destroyed
after each execution — so they use poll mode. It covers the same chats, just on
the 30-minute cadence instead of instantly.

Tune it under `telegram_user:` in `config.yml`. By default it reads every group
and channel you have joined, skips your 1:1 DMs, and requires a message to
contain both a hiring word *and* a technical keyword (VoIP, SIP, Issabel,
Asterisk, IT support, Linux, Odoo, POS, telecom …) before it costs any Gemini
quota. Narrow it with `include_chats`, or set `require_tech_match: false` to
widen the net.

The client is **read-only**: it never sends, joins, forwards or reacts. It
paces itself between chats and obeys Telegram's `FloodWaitError` rather than
retrying through it. That keeps it well clear of the limits, though no
automation on a personal account is entirely without risk.

### Adding public Telegram channels

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
| `python main.py --live` | Real-time Telegram listener + periodic sweeps |
| `python main.py --stats` | Lifetime statistics and recent run history |
| `python main.py --selftest` | Verify Gemini + WhatsApp connectivity |
| `python main.py --prune` | Compact the deduplication database |
| `python setup_wizard.py` | Full setup verification |
| `python discover_channels.py` | Audit public Telegram channels |
| `python auth_telegram.py` | One-time Telegram login (private groups) |
| `python check_telegram.py` | Verify the Telegram client, list your groups |
| `python -m unittest discover -s tests` | Offline test suite (97 tests, no network) |

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
  ├─ telegram_user_client.py  MTProto user client: your joined private
  │                     groups, poll + live event modes
  ├─ talent.py          talent.com regional boards
  ├─ tanqeeb.py         Tanqeeb: 7 Arab subdomains, Arabic search,
  │                     JSON-LD description enrichment
  ├─ job_apis.py        Six free JSON APIs
  ├─ search_proxy.py    Google News RSS for 403-blocked boards
  ├─ rss_feeds.py       Generic RSS/Atom
  └─ facebook.py        Indexed posts / optional cookie
setup_wizard.py         Setup, verification, secret export
auth_telegram.py        One-time interactive Telegram authorisation
check_telegram.py       Telegram connection/dialog/filter verification
discover_channels.py    Public Telegram channel auditor
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
