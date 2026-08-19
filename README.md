# AI Job Hunter

An autonomous job-hunting bot. It watches LinkedIn, Telegram, GCC job boards
and a set of free job APIs around the clock, scores every posting against your
CV with Gemini, and sends you a WhatsApp message the moment something scores
75% or higher.

It runs entirely on GitHub's infrastructure. **Your computer can be off.**

Every match arrives on **both** channels, tied together by a short reference:

```
WHATSAPP — light, bilingual, no URL          TELEGRAM — full master card
──────────────────────────────────           ──────────────────────────────────
🚨 NEW HIGH-MATCH JOB #101 (95%)             🚨 NEW HIGH-MATCH JOB #101  (95%)
🏢 Company: Etisalat                         🏢 Company:  Etisalat
💼 Role: VoIP Support Engineer               💼 Role:     VoIP Support Engineer
📍 Location: Dubai, UAE                      📍 Location: Dubai, UAE
💰 Salary: 12,000-15,000 AED per month       💰 Salary:   12,000-15,000 AED
📡 Source: linkedin                          📡 Source:   linkedin

📝 مهندس دعم شبكات VoIP لإدارة السنترالات    🔗 Link: https://ae.linkedin.com/...
✅ خبرة ممتازة في Asterisk وIssabel
⚠️ سنوات الخبرة أقل من المطلوب                ✅ Why you match: Directly matches
                                                Asterisk and Issabel PBX work.
🔗 Link: Search Telegram Saved              ⚠️ Gaps: under 2 years experience
   Messages for #101
                                             📝 (same Arabic read-out)
                                             🔎 Reference: #101
```

**The WhatsApp card carries no link on purpose.** CallMeBot sends the message
in a query string and *drops* whatever overflows its URL ceiling rather than
truncating. Job URLs run past 400 characters and Arabic costs ~5.6 URL
characters each — carrying both would blow the budget and lose the alert
silently. Dropping the link buys back the room the Arabic needs, and `#101`
gets you to the full card in Telegram.

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

A representative live run:

```
2,758 scraped  →  2,077 recent  →  195 never seen  →  192 candidates
                                                       →  120 evaluated  →  3 alerts
```

(The 195 "never seen" are exactly the 195 postings the *previous* run deferred
past its evaluation cap — deferral and re-queue working end to end.)

Every stage is counted in `run_report.json`, so you can always see where a
posting was lost. The funnel exists because Gemini's free tier is metered per
minute *and* per day: the cheap filters do the bulk elimination so the expensive
stage only ever sees postings that are both new and plausible. Anything over the
per-run cap is **deferred, not discarded** — those postings are deliberately left
unrecorded so the next run picks them up.

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

### What changes when it runs in the cloud

GitHub Actions runs from datacentre IPs, and two services treat those
differently from a home connection. Both were found by reading a real cloud run,
not by guessing:

| | Local / VPS | GitHub Actions | Handling |
|---|---|---|---|
| **CallMeBot (WhatsApp)** | HTTP 200 | **HTTP 403** | Falls back to Telegram Saved Messages |
| **Tanqeeb** | 928 postings | **0** (all subdomains blocked) | Circuit breaker fails it fast in ~18s |
| Everything else | works | works | — |

The CallMeBot block is the one that mattered: a scheduled run found **9 genuine
matches and delivered none of them**, with `alerts_failed: 9` the only trace.
Alerts now fall back to your Telegram Saved Messages, which has no such
restriction and reuses the MTProto session the bot already holds. Set
`notifications.telegram_fallback: false` to disable.

If you want Tanqeeb's GCC coverage as well, run the bot on a VPS or in Docker
(`python main.py --daemon`) rather than on Actions — see DEPLOYMENT.md.

### Source health & audit digest

Proof that every platform actually ran, delivered to WhatsApp:

```
📊 SOURCE HEALTH & AUDIT REPORT
──────────────────
🔹 Tanqeeb (Arab/GCC): 928 jobs scraped
└ Last seen: "IT Help Desk Specialist @ Erada Egypt"
🔹 Talent.com Regional: 640 jobs scraped
└ Last seen: "Telecom Support Engineer @ Solutions by STC"
🔹 LinkedIn (GCC): 369 jobs scraped
└ Last seen: "VoIP Engineer @ Etisalat"
🔻 RSS Feeds: FAILED
└ HTTP 429
──────────────────
Total: 2,769 jobs inspected across 8/9 platforms.
```

Run it on demand — ingestion only, no AI calls, nothing recorded as seen, so it
is safe to run any time and cannot consume the evaluation backlog:

```bash
python main.py --digest
```

It also fires automatically. Not on every run: at a 30-minute cadence that
would be ~48 messages a day and would bury the job alerts it exists to support.
Instead:

| Trigger | When |
|---|---|
| Routine | Every `digest_interval_hours` (default 12) — standing proof of reachability |
| Failure | **Immediately** when a source breaks or returns nothing, throttled by `digest_failure_cooldown_minutes` |

Failed sources sort to the top, because they are the only part of the report
that needs acting on. Set `digest_interval_hours: 0` to send one after every run.

**On message limits.** CallMeBot carries the whole message in a query string
with a hard URL ceiling, and it *drops* what it cannot fit rather than
truncating. Budgeting counts **encoded** length, not characters — Arabic is two
bytes per character and about nine URL characters once percent-encoded, so a
509-character Arabic alert is 1,985 URL characters. The digest therefore splits
across numbered parts rather than dropping a source, since a truncated audit
would misreport a working platform as absent.

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

By default it reads every group and channel you have joined, which is rarely
what you want: a real dialog list is mostly news, deals and social channels. Let
it tell you which ones actually pay their way, then narrow it:

```bash
python check_telegram.py --scan 168 --suggest
```

That ranks each monitored chat by how many hiring posts it genuinely produced
over the window and prints an `include_chats:` block to paste into `config.yml`.
Fewer chats means faster runs and less flood-wait risk, with no loss of coverage.

Tune it under `telegram_user:` in `config.yml`. It skips your 1:1 DMs, and requires a message to
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
| `python main.py --digest` | Audit every source now, WhatsApp the report |
| `python main.py --stats` | Lifetime statistics and recent run history |
| `python main.py --selftest` | Verify Gemini + WhatsApp connectivity |
| `python main.py --prune` | Compact the deduplication database |
| `python setup_wizard.py` | Full setup verification |
| `python discover_channels.py` | Audit public Telegram channels |
| `python auth_telegram.py` | One-time Telegram login (private groups) |
| `python check_telegram.py` | Verify the Telegram client, list your groups |
| `python check_telegram.py --scan 168 --suggest` | Rank your chats by hiring output, emit a narrowed `include_chats` |
| `python -m unittest discover -s tests` | Offline test suite (115 tests, no network) |

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
