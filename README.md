# flight-agent

Monitors Google Flights daily and emails a deal summary when prices drop
below your configured thresholds.

---

## Architecture and execution flow

```
GitHub Actions (cron: 05:00 UTC daily)
        │
        ▼
flight_checker.py
  1. Validate required env vars
  2. Load ~/.flight_agent/state.json  ← persisted via actions/cache
  3. Prune expired dedup records (> 60 days old)
  4. Load searches.json, expand "ALL" destinations
  5. For each search / date pair:
       a. Check monthly quota guard (stop early if hit)
       b. Open Google Flights page in headless Chromium (Playwright)
       c. Dismiss consent dialogs, wait for prices to render
       d. Extract and filter deals (price + departure hour)
       e. Retry up to 3× with exponential back-off on failure
       f. Compare each deal against seen_deals in state
       g. Mark new deals as seen; skip already-emailed deals
  6. Save updated state.json
  7. Build plain-text + HTML email; send via Gmail SMTP
  8. Exit non-zero if any search produced an error
        │
        ▼
Gmail inbox  ←  HTML + plain-text email with deal details
```

State (`~/.flight_agent/state.json`) is cached between workflow runs with
`actions/cache`, keyed by run number, so deduplication and quota tracking
survive across days without committing anything to the repository.

---

## Selected free services and why they fit

| Service | Purpose | Why free |
|---------|---------|----------|
| **GitHub Actions** (self-hosted runner) | Daily scheduling + execution | Self-hosted runners have no minute limit |
| **Google Flights** (browser scraping) | Flight data | No official API; Playwright page loads incur no quota cost |
| **Gmail SMTP** (App Password) | Email delivery | Free for personal send volume |
| **`actions/cache`** | State persistence | Free, included in GitHub Actions |

No credit card is required for any of these.

---

## Monthly quota calculation

| Metric | Value |
|--------|-------|
| Daily scheduled runs | 1 |
| Searches per run | 44 (4 AMS + 4 MUC + 36 ALL destinations) |
| Expected page loads per run | 44 |
| Expected monthly page loads (31 days) | 1,364 |
| Retry / testing allowance (+47%) | 636 |
| **Configured `MAX_MONTHLY_REQUESTS`** | **2,000** |
| Provider's verified free monthly limit | Unlimited (self-hosted runner + browser scraping) |
| Safety margin vs hard cap | 636 page loads / 31% headroom |

> **Note on "API limits":** This agent scrapes Google Flights pages via
> Playwright, not through a paid API. There is no metered quota from Google.
> `MAX_MONTHLY_REQUESTS` is an internal safety guard against infinite loops
> or misconfigurations, not an external billing constraint.

*Limits last verified: 2026-10-27*
*GitHub Actions self-hosted runner pricing: https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions*

---

## Setup

### Prerequisites

- Python 3.12+
- A Gmail account with 2-Step Verification enabled
- A self-hosted GitHub Actions runner configured on your machine
- Git and the GitHub CLI (optional, for manual triggers)

### 1. Gmail App Password

1. Enable 2-Step Verification on your Gmail account:
   `myaccount.google.com/security`
2. Generate an App Password:
   `myaccount.google.com/apppasswords`
   - App: "Mail"  — Device: "Other (flight-agent)"
3. Copy the 16-character code — you will not see it again.

### 2. GitHub repository secrets

Go to your repository → **Settings → Secrets and variables → Actions → Secrets**
and add these three secrets:

| Secret name | Value |
|-------------|-------|
| `EMAIL_FROM` | Your Gmail address (e.g. `you@gmail.com`) |
| `EMAIL_TO` | Recipient address (can be the same) |
| `EMAIL_APP_PASSWORD` | The 16-character App Password from step 1 |

### 3. Optional repository variables

Go to **Settings → Secrets and variables → Actions → Variables** to set
these optional controls:

| Variable | Default | Description |
|----------|---------|-------------|
| `SEND_NO_DEALS_EMAIL` | `true` | Set to `false` to skip email on days with no new deals |
| `MAX_MONTHLY_REQUESTS` | `2000` | Monthly page-load cap (safety guard) |

### 4. Self-hosted runner

If you haven't configured your runner yet:

1. Go to **Settings → Actions → Runners → New self-hosted runner**
2. Follow the instructions for your OS
3. Start the runner service (it must be running when the workflow triggers)

### 5. Clone and configure searches

```bash
git clone https://github.com/<your-org>/flight-agent.git
cd flight-agent
```

Edit `searches.json` to define your routes and dates (see
[Search criteria](#search-criteria) below), then push.

---

## Required environment variables and secrets

| Name | Required | Where to set | Description |
|------|----------|-------------|-------------|
| `EMAIL_FROM` | Yes | GitHub secret | Sender Gmail address |
| `EMAIL_TO` | Yes | GitHub secret | Recipient email address |
| `EMAIL_APP_PASSWORD` | Yes | GitHub secret | Gmail App Password (16 chars) |
| `SEND_NO_DEALS_EMAIL` | No | GitHub variable | `true`/`false` (default `true`) |
| `MAX_MONTHLY_REQUESTS` | No | GitHub variable | Integer cap (default `2000`) |
| `DEDUP_TTL_DAYS` | No | Environment | Days to remember a deal (default `60`) |
| `FLIGHT_AGENT_STATE_FILE` | No | Environment | Custom path for state JSON |

---

## Search criteria

Searches are defined in `searches.json` as an array of objects:

```json
[
  {
    "origin": "TLV",
    "destination": "AMS",
    "max_price": 650,
    "max_depart_hour": 14,
    "date_pairs": [
      ["2027-10-12", "2027-10-21"],
      ["2027-10-13", "2027-10-20"]
    ]
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `origin` | string | IATA departure airport code |
| `destination` | string | IATA arrival code, or `"ALL"` to check all ~36 European airports |
| `max_price` | integer | Maximum round-trip price in USD |
| `max_depart_hour` | integer | Latest acceptable departure hour (0–23, 24-hour clock) |
| `date_pairs` | array | List of `[outbound, return]` date strings (YYYY-MM-DD) |

Setting `"destination": "ALL"` expands to all airports in
`EXPLORE_DESTINATIONS` (currently 36 European cities).

---

## Configuring sender and recipient addresses

Set `EMAIL_FROM` and `EMAIL_TO` as GitHub repository secrets (see Setup
step 2). They can be the same address.

For local runs, create a `.env` file based on `.env.example`:

```bash
cp .env.example .env
# Edit .env and fill in your real values
```

---

## Daily schedule and timezone

The workflow runs at **05:00 UTC = 08:00 Israel time (UTC+3)** via the
cron expression `0 5 * * *` in [`.github/workflows/main.yml`](.github/workflows/main.yml).

All times in the cron schedule are **UTC**. To change the schedule:

1. Edit the `cron:` field in `.github/workflows/main.yml`
2. Use [crontab.guru](https://crontab.guru) to verify your expression
3. Commit and push — the new schedule takes effect on the next run

One execution runs per day. The `concurrency` group in the workflow
prevents overlapping runs if a manual trigger and a scheduled trigger
fire at the same time.

---

## Running locally

```bash
# 1. Install dependencies
pip install -r requirements-dev.txt
playwright install chromium --with-deps

# 2. Create your .env file
cp .env.example .env
#    Fill in EMAIL_FROM, EMAIL_TO, EMAIL_APP_PASSWORD

# 3. Run (reads .env if you use python-dotenv, or export manually)
export $(grep -v '^#' .env | xargs)
python flight_checker.py
```

State is stored at `~/.flight_agent/state.json` by default. To use a
different path:

```bash
FLIGHT_AGENT_STATE_FILE=/tmp/test-state.json python flight_checker.py
```

---

## Safe manual test run

To trigger the workflow without sending a real email, use one of:

**Option A — GitHub UI:**
1. Go to **Actions → Daily Flight Check**
2. Click **Run workflow → Run workflow**
3. Watch the run log; the email lands in `EMAIL_TO`

**Option B — GitHub CLI:**
```bash
gh workflow run main.yml --repo <owner>/flight-agent
```

**Option C — Local dry run (no email):**
```bash
SEND_NO_DEALS_EMAIL=false \
FLIGHT_AGENT_STATE_FILE=/tmp/dry-run-state.json \
python flight_checker.py
```

This runs all searches and prints results but skips the email step when
no new deals are found. Use a throw-away state file so it doesn't affect
real deduplication state.

---

## Confirming an email was sent

1. **GitHub Actions log** — look for `Email sent → <address>` near the end
   of the "Run flight checker" step.
2. **Sent folder** — check the Gmail sent folder of `EMAIL_FROM`.
3. **Inbox** — check `EMAIL_TO`. If not in inbox, check spam/promotions.

---

## Deduplication and state persistence

A deal is identified by the stable key:

```
SHA-256(origin | destination | outbound_date | return_date | airline | price)[:12]
```

When a deal is emailed, its ID is written to `~/.flight_agent/state.json`
along with a timestamp. Subsequent runs skip any deal whose ID is already
in this file.

**Retention:** IDs are pruned after `DEDUP_TTL_DAYS` days (default 60).
This means:

- The same deal at the same price will not be emailed again for 60 days.
- If a price drops (different amount), it is treated as a new deal and
  emailed again.
- After 60 days, a deal may appear again if it is still available.

**State persistence in GitHub Actions:**
The `~/.flight_agent/` directory is cached using `actions/cache` with a
unique key per workflow run number. The restore step picks up the most
recent prior snapshot. GitHub automatically evicts cache entries after
7 days of non-use or when total cache storage exceeds 10 GB — both
limits are far beyond what this agent requires.

---

## Monthly limit behaviour

When `MAX_MONTHLY_REQUESTS` is reached mid-run:

1. The script logs a warning and stops launching new browser pages.
2. Any deals found so far (for completed searches) are still included in
   the email.
3. The state is saved with the current quota count.
4. The email is sent normally with partial results.
5. The quota counter resets automatically at the start of the next
   calendar month (UTC).

To check current usage, look for the log line:

```
Monthly requests used this month: X / 2000
```

---

## Limitations and troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No email received | Missing/wrong secret | Check `EMAIL_FROM`, `EMAIL_TO`, `EMAIL_APP_PASSWORD` in Actions secrets |
| `KeyError` on startup | Missing env var | Re-check secrets are set on the correct repository |
| `535 authentication failed` | Wrong app password or 2FA disabled | Regenerate App Password; ensure 2FA is on |
| Empty results / debug screenshots | Google showing CAPTCHA or consent | Check debug-screenshots artifact in the run; may self-resolve next day |
| Same deals emailed every day | State file not persisted | Confirm `actions/cache` step ran; check runner has write access to `~` |
| Workflow not triggering | Runner offline | Ensure the self-hosted runner service is running |
| `playwright` not found | Dependencies not installed | Run `pip install -r requirements.txt && playwright install chromium --with-deps` |

---

## Deploying

The agent is deployed by pushing to the `main` branch. The workflow
reads from the repository at checkout time, so:

```bash
# Edit searches, schedule, or code
git add .
git commit -m "Update searches"
git push origin main
```

No additional deployment step is required — GitHub Actions picks up the
change on the next scheduled or manual trigger.
