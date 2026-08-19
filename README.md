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

## Phase 2 — auto-apply & interview copilot

Local only. It needs a real browser and it writes to a private vault, so it
never runs on the cloud schedule — that stays a pure discovery pipeline.

```bash
python main.py --provision          # vault credentials for every board (offline)
python main.py --vault              # what is stored + the profile payload
python main.py --register Tanqeeb   # signup: pre-fills, submits unless CAPTCHA
python main.py --apply              # draft applications for top matches
python main.py --applications       # what is drafted / submitted
python main.py --approve 1          # approve a draft, then submit it
python main.py --decline 1          # discard it
python main.py --listen             # approve/edit by REPLYING on Telegram
python main.py --listen-once        # process replies once, then exit (cron)
python main.py --inbox              # scan the mailbox for interview mail
python main.py --watch-inbox        # keep scanning
```

### Approve and edit by reply, on WhatsApp and Telegram

A drafted application is pushed to **both** channels, and you answer it from
wherever you happen to be. The terminal commands above still work; they are no
longer the only way in, because in practice the draft is read on a phone hours
before the terminal is next opened, and the posting has often closed by then.

```
📝 APPLICATION DRAFT READY FOR REVIEW [DRAFT #1]

🏢 Company:  Etisalat
💼 Role:     VoIP Engineer
🌐 Platform: tanqeeb:uae
📊 Match:    95%

📋 PROPOSED FORM ANSWERS
💰 Salary:     12,000 AED
🧮 Experience: 3 years
1. How many years have you used Asterisk?
   → Three years.

✍️ TAILORED COVER LETTER
I have run Issabel and Asterisk PBX estates for three years...
```

Reply with any of these, in Arabic or English:

| Reply | What happens |
|---|---|
| `done` / `done 1` / `موافق ١` / `اعتمد 1` | Playwright submits it, captures a full-page screenshot, marks it `submitted`, and confirms on both channels with the image |
| `edit 1 salary: 18000 AED` | rewrites the draft **and** the value that will be typed into the form, then re-sends the card |
| `edit 1 cover letter: <new text>` | same, for the letter |
| `edit 1 answer 2: <new answer>` | replaces one screening answer |
| `edit 1: make it shorter` | free-form — Gemini applies the instruction to the letter |
| `تعديل ١ الراتب: ١٥٠٠٠` | any of the above, in Arabic, with Arabic-Indic digits |
| `decline 1` / `رفض ١` | discards the draft |
| `status` / `الحالة` | what is waiting for you |
| `help` / `مساعدة` | the full syntax |

Four things this is careful about, each of which is a way it could go wrong:

* **An edit changes what is SUBMITTED, not just what is shown.** The card reads
  `cover_letter_text`; the browser types `submitted_payload_json["fields"]`.
  Both are rewritten from one instruction, because updating only the first
  produces a draft that looks edited and submits the original.
* **An edit always returns the draft to `review_pending`,** so the next `done`
  confirms the version you actually read.
* **A bare `done` with several drafts pending refuses to guess.** It lists them
  and asks for a number — submitting cannot be undone. With exactly one draft
  pending it just works. (`hitl.confirm_when_ambiguous: false` to change that.)
* **The bot never obeys its own cards.** Every card carries an invisible marker,
  because the card itself contains the line `done 1` as *instructions* — and it
  is delivered to the same Saved Messages the listener reads.

Commands are read from **Telegram only**. CallMeBot is send-only: there is no
way to receive a WhatsApp reply through it, so WhatsApp carries the card and
Telegram carries the conversation. Tuning lives under `hitl:` in `config.yml`.

**Replying on WhatsApp needs a relay.** CallMeBot — which carries every
outbound WhatsApp card here — is *send-only*: it has no receive endpoint at
all. So `auto_apply/inbound.py` exposes a webhook that accepts inbound messages
from whatever relay you can get: Meta's WhatsApp Cloud API (verified by
`X-Hub-Signature-256`), Twilio (form-encoded), or anything you control that can
POST JSON — a Baileys bridge, n8n, a phone shortcut. All three normalise to the
same command and run the same pipeline as Telegram. Configure it under
`hitl.whatsapp_inbound`; until you do, `--listen` says so explicitly rather
than claiming a channel it cannot receive on.

That endpoint can submit a job application, so it **fails closed**: no secret
configured means every request is refused, a bad HMAC is refused, a message
from any number but yours is refused, and a re-delivered message never executes
twice.

**Keeping the listener alive.** `--listen` is a foreground process: it dies
with whatever started it — a terminal you close, a dropped SSH session, an
agent session that ends. That matters because the failure is *silent*: the
review card still arrives, you still reply, and nothing happens. So run it
under the supervisor instead, which restarts on crash and detaches from the
console:

```powershell
# run it here, Ctrl-C to stop
powershell -ExecutionPolicy Bypass -File scriptsun_listener.ps1

# or detached -- closing the window leaves it running
powershell -ExecutionPolicy Bypass -File scriptsun_listener.ps1 -Detached

# or at every logon, surviving a reboot (see the script's header for the
# exact schtasks line)
schtasks /Run /TN "AI Job Hunter Listener"
```

It refuses to start a second copy: two listeners on one Telegram session fight
over the update stream and both act on the same command. A clean Ctrl-C is
respected rather than restarted — a supervisor you cannot stop is worse than
none.

**`--listen` runs two mechanisms, not one.** An event handler reacts the instant
you reply, and a poll sweep re-checks every 60s. That redundancy is deliberate:
Telegram never delivers an update for a message the *same* session sent, so the
event handler cannot be verified from inside the bot — only by you typing on
your phone, and by then it is too late to discover it was not wired up. Silent
inaction on an approval is the worst failure this component has. Both routes
share one forward-only cursor, so nothing is ever executed twice.

### Fourteen boards, one saved login each

| Region | Boards |
|---|---|
| Egypt & Gulf | Tanqeeb (all 7 countries), Wuzzuf, Bayt, GulfTalent, Naukrigulf, Forasna, Akhtaboot |
| Global | Talent.com, Indeed, Glassdoor, ZipRecruiter, Foundit/Monster, RemoteOK, WeWorkRemotely |

Each board declares the **hosts** it serves, so a scraped job URL resolves back
to the login that can open it. Matching is on the registrable domain, never a
substring — `notbayt.com.evil.test` cannot borrow Bayt's cookies.

**This is what unblocks applying at all.** Signed out, these boards serve a
public landing page whose only form is the site search — which the submit gate
correctly refuses. `--register <board>` saves the authenticated session to
`secrets/sessions/<board>_state.json`, and `--apply` / `--approve` then open the
job page as a signed-in candidate, where the real application form exists.

Two layers of persistence, doing different jobs: a Chromium profile directory
(keeps the login on this machine, including localStorage and device
fingerprint) plus a portable `storage_state` JSON snapshot (survives a wiped
profile, and can be copied or deleted per board).

### Multi-step ATS forms

`browser.py` recognises Workday, Greenhouse, Lever, SmartRecruiters, Taleo,
iCIMS, Ashby, BambooHR, Recruitee, Workable and Jobvite — from the URL first,
then from the markup, which catches an ATS iframed onto the employer's own
domain. Wizard-style ATSes are walked page by page: fill what is visible, click
Continue, fill the next page, and stop the moment a real submit control
appears. `Next` and `Submit` selectors are kept strictly disjoint, because
clicking submit when you meant next files a half-empty application.

The CV goes in by three routes in order — the detected field, the page's first
(usually hidden) file input, then the OS file chooser. Route two is the one
that matters: Greenhouse and Lever style a `<div>` over the real input, so the
visible control cannot be filled.

Ten standard screening questions (notice period, expected salary, years with
VoIP/Asterisk/Python/AI, sponsorship, relocation, start date) are drafted up
front and matched onto the board's own wording by topic — a wizard's page three
says "Notice period (weeks)" where the draft says "What is your notice period?",
and exact matching would leave it blank and fail validation.

### Platform provisioning

`--provision` derives a **different** password per board from one seed
(HMAC-SHA256) and vaults platform, email, username and password. It opens
nothing and creates nothing, so it is safe to run for every board at once — and
running it first means an interrupted signup still leaves a recoverable
credential rather than one that existed only in a browser window you closed.

| Board | Signup |
|---|---|
| Tanqeeb, Wuzzuf, Talent.com, GulfTalent | pre-filled and submitted automatically |
| Bayt, Naukrigulf | pre-filled, **never** auto-submitted (`manual_signup`) |

`--register <board>` fills everything the inspector can name — contact details,
the professional headline, the bio, the CV upload — then submits, **unless** a
human-verification challenge is on the page, in which case it stops and hands
over. Pushing through a CAPTCHA is what turns "an account" into "a banned
account on a board the job hunt depends on". Bayt and Naukrigulf are marked
manual because they verify by SMS, where a half-created account burns the email
address. Set `auto_submit_registration: false` to always stop and confirm.

### The vault is a separate database, on purpose

`state/jobs.db` is force-pushed to a **public** branch every run. Credentials,
cover letters and recruiter mail therefore live in `state/vault.db` — git-ignored,
never touched by the workflow, encrypted with Fernet. A test asserts the
workflow never copies it, so a future edit cannot quietly start publishing it.

### Three deliberate limits

**LinkedIn is never automated.** Its enforcement is the strictest of any source
here and it is also the most productive one; a flagged account would cost far
more than the applications it saved. LinkedIn matches are alert-only, refused at
every entry point rather than filtered somewhere downstream.

**Registration is assisted, not automatic.** Signup flows sit behind CAPTCHA and
phone verification. The browser opens the page, derives a strong per-platform
password and fills everything from your CV; you solve the CAPTCHA and press
submit. Same finished profile, none of the ban risk. Accounts you made by hand
can be vaulted directly.

**Approval is a gate, not a notification.** A draft sits at `review_pending`
until you approve it by id. Gemini writes the cover letter and the answers, but
nothing is submitted in your name on the strength of a model's guess at your
salary expectations. Set `require_approval: false` to change that.

### It refuses to submit into the wrong form

Job pages are full of forms that are not the apply form. On a live Tanqeeb page
the inspector found the site's **search widget** — filling that and clicking
submit would have run a search and reported it as a submitted application. So a
form must show real evidence (a CV upload, a cover-letter box, or two personal
fields) before anything is submitted; otherwise the draft is kept, the cover
letter is ready to paste, and you get the link to apply by hand.

The check is **fail-closed and pre-flight**: only a draft whose stored
`form_ok` is explicitly `true` may reach a browser at all. It used to refuse
only on an explicit `false`, which let two cases through to Chromium — a draft
written before `form_ok` was recorded, and one with no payload. Neither is
hypothetical: the pending draft in the vault had `form_ok: null` and its only
detected fields were `keywords` and `state`, i.e. Tanqeeb's search box. There
is still a second check *inside* the browser in case the page changed between
drafting and approval, but it is a backstop, not the gate.

### Interview monitor

Recruiter mail is classified by Gemini into interview / assessment / rejection /
acknowledgment, cross-referenced against your applications, and pushed to
Telegram with the meeting time and joining link already extracted.

**Two ways in, chosen automatically:**

| Backend | When | Setup |
|---|---|---|
| **Gmail API (OAuth2)** | preferred; the only option on newer accounts | `python auth_gmail.py` |
| IMAP + App Password | older accounts that still have one | `JOB_EMAIL_APP_PASSWORD` in `.env` |

Google has stopped issuing App Passwords on newly created accounts — the
setting is simply absent — so OAuth2 is the supported path. It is also better:
the token is scoped to Gmail alone and can be revoked without changing any
password.

```bash
python auth_gmail.py            # one-time browser consent
python auth_gmail.py --status   # what is authorised
python auth_gmail.py --revoke   # delete the local token
```

One-time Google Cloud setup, because OAuth identifies the *application* and no
credential for that can ship in a public repo:

1. [console.cloud.google.com](https://console.cloud.google.com/) → new project
2. APIs & Services → Library → enable **Gmail API**
3. OAuth consent screen → External → add your address under **Test users**
4. Credentials → Create → **OAuth client ID** → *Desktop app*
5. Save the JSON as `secrets/gmail_client_secret.json`

Both the client secret and the resulting token are git-ignored; a test asserts
neither can be committed and that no client secret is embedded in the source.

It is careful with a real mailbox either way. `messages.get` does not mark
anything read (nor does IMAP's `BODY.PEEK`) — only an explicit label change
does, and only for mail already classified as job-related. A cheap local filter
runs first, so a personal email is never sent to the AI at all.

**One alert per email, and one Gemini call per email — for life.** The monitor
re-reads the same mailbox every 15 minutes, so both guarantees are about what
happens on the *second* pass:

- `message_id` is `UNIQUE` in `email_interview_events` and `seen_message()`
  reads that table, so a message is classified at most once, ever. Every
  verdict is banked — **including "not job mail"**. Skipping that record meant
  a job-alert digest, which says "position" and so clears the keyword gate on
  every pass, was re-sent to Gemini on every poll for as long as it sat unread:
  roughly 96 wasted calls a day, indefinitely.
- The event row is written **before** the alert is sent. That ordering is what
  makes a duplicate impossible even if the process dies mid-send or two polls
  overlap. `alerted` is then corrected to what actually happened, so it means
  "you saw it" rather than "we meant to tell you"; a failed send is logged
  loudly rather than retried, because a second copy of an interview invitation
  is worse than one you can find in the log.

A transient Gemini error is *not* banked — only real verdicts stick — so a 503
costs a retry next pass rather than losing the invitation.

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
| `python main.py --listen` | Approve/edit drafts by replying on Telegram |
| `python main.py --listen-once` | Process new replies once, then exit (cron) |
| `python main.py --provision` | Derive + vault credentials for every board |
| `python main.py --vault [--reveal]` | Show the vault and the profile payload |
| `python -m pytest` | Offline test suite (860 tests, no network) |

---

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

**Use pytest, not `unittest discover`.** The safety harness lives in
`tests/conftest.py`, which only pytest loads. Running the suite any other way
skips it, and `tests/test_no_real_sends.py` will refuse to import rather than
let that happen quietly.

### The suite cannot message you

It used to. Every module set `DRY_RUN=true`, all assertions passed, the run
finished green — and five real job cards landed in the user's Telegram Saved
Messages on **every run**, because `send_via_telegram` had no dry-run guard and
`dispatch()` called it in both arms of an `if dry_run: … elif …`. A green suite
proved nothing about delivery.

That is fixed in `notifier.py`, and `conftest.py` now makes the next such bug
unable to reach you, through three independent layers:

| Layer | What it does |
|---|---|
| **Environment** | Real credentials are replaced with obvious test values *before* `config.py` reads `.env`, so a stray call authenticates as nobody. `TELEGRAM_STRING_SESSION` is emptied outright. |
| **Transports** | `send_via_telegram`, `send_photo_via_telegram`, `send_raw`, `_send_callmebot` and `http_client.get` become recorders that deliver nothing. Ask for the `outbox` fixture to assert on what *would* have been sent, by channel. |
| **Sockets** | `connect()` to any non-loopback address raises. Loopback stays open because asyncio's self-pipe needs it. |

The socket layer is the one that matters — the first two can be defeated by a
test that builds its own client; a blocked socket cannot.
`tests/test_no_real_sends.py` asserts all three are armed *and* that the real
production methods honour `DRY_RUN`, checked against the genuine
implementations rather than the stubs. It also scans every module for a direct
`send_message(` or `send_file(` call, so a new route into your Saved Messages
cannot appear without going through the one guarded method — and it drives the
review listener against its own cards, to prove the bot cannot approve an
application by reading the card it just sent.

No secrets are supplied to CI on purpose: a suite that needs a real key to pass
has a bug, and it should surface there rather than in your Telegram.

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
vault.py                Encrypted local store: credentials, applications, inbox events
auto_apply/             Phase 2: registration, drafting, approval, submission
  ├─ gmail_oauth.py     Gmail API OAuth2: token lifecycle and refresh
  ├─ email_listener.py  Inbox monitor, backends, exactly-once alerting
  ├─ engine.py          Draft → review → approve → submit → evidence
  ├─ review.py          The review card per channel + the in-line edit engine
  ├─ commands.py        The Arabic/English reply grammar ("done 7", "تعديل ...")
  ├─ control.py         What a reply DOES + the Saved-Messages listener
  ├─ browser.py         Playwright plumbing, semantic form + CAPTCHA detection
  ├─ profile_builder.py Assisted account creation, per-platform passwords
  └─ candidate.py       Structured profile extracted from the CV
setup_wizard.py         Setup, verification, secret export
auth_gmail.py           One-time Gmail API authorisation
auth_telegram.py        One-time interactive Telegram authorisation
check_telegram.py       Telegram connection/dialog/filter verification
discover_channels.py    Public Telegram channel auditor
tests/                  Offline suite -- run with pytest, never unittest
  ├─ conftest.py        The safety harness: env, transports, sockets
  ├─ test_no_real_sends.py   Proof the suite cannot message you
  ├─ test_scrapers_audit.py  Every ingestion source against fixed markup
  ├─ test_evaluator_ai.py    Prompt, schema, retries, quota exhaustion
  ├─ test_db_integrity.py    Dedup keys, migrations, rollback, encryption
  ├─ test_browser_forms.py   Form inspector, CV mapping, CAPTCHA, screenshots
  ├─ test_gmail_oauth.py     Token refresh, IMAP fallback, inbox triage
  ├─ test_hitl_commands.py   The reply grammar, recall AND precision
  ├─ test_hitl_review.py     Both review cards + the in-line edit engine
  ├─ test_hitl_controller.py Approve/edit/decline + the listener's gates
  ├─ test_hitl_e2e.py        draft → edit by reply → done → submit → evidence
  ├─ test_inbox_dedup.py     Exactly-once alerting, Gemini quota
  └─ test_lifecycle_e2e.py   register → apply → approve → evidence
pytest.ini              testpaths=tests, warnings are errors
n8n_workflow.json       Importable n8n visual workflow
.github/workflows/      The 24/7 GitHub Actions engine + the test gate
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
