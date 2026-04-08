# pathway-rfd

End-to-end restaurant RFP pipeline for the Pathway take-home exercise.

## Overview

This project automates the RFP workflow:

1. Parse a menu into structured recipes + ingredients
2. Fetch pricing trend references for ingredients
3. Find local distributors and map likely coverage
4. Send RFP outreach (email/contact form)
5. Monitor inbox replies, parse quotes, follow up for missing info, and compare options

## Typical Flow

1. Step 1: upload a menu image and parse

- The image is sent to the LLM parser (Anthropic primary, OpenAI fallback) with a structured schema prompt.
- The parser extracts all visible dishes and infers realistic ingredient lists and per-serving quantities.
- Results are stored in `recipes`, `ingredients`, and `recipe_ingredients`.

2. Step 2: fetch market trends

- Fetches BLS price data (used here as a market trend proxy) and caches series data by month.
- On reruns, it reuses cache if current-month data already exists.
- Ingredient names are matched to BLS items via LLM matching and fallback logic.
- Structured trend metadata/tags are stored with each ingredient pricing record.

3. Step 3: find distributors

- Uses Serper (Google Places) to find distributors for required ingredient categories in the selected area.
- Stores distributors and their ingredient links in `distributors` and `distributor_ingredients`.
- Scrapes websites for contact emails or contact forms; falls back to phone-only when needed.

4. Step 4: send outreach (`Dry Run` or `Live`)

- Sends RFP emails (from your configured Gmail sender) to distributors with email contacts.
  - If `Dry Run`, recipients are redirected to Yopmail inboxes.
- Attempts contact-form outreach using `MechanicalSoup` with pattern mapping and LLM fallback for field mapping.
  - In `Dry Run`, forms are filled and reported but not submitted.
- Phone-only vendors are marked as skipped for manual follow-up.

5. Step 5: check inbox and review provider coverage/pricing

- Reads replies from Gmail, matches them to contacted distributors, and parses quoted details.
- Updates `distributor_ingredients` with:
  - confirmed items and quoted prices
  - explicit does-not-supply responses
  - clarification-needed items
- Automatically sends:
  - thank-you when complete
  - follow-up requests when details are missing
- UI supports both:
  - `By Provider` status view (`confirmed`, `unconfirmed`, `does_not_supply`)
  - `By Ingredient` comparison view with coverage snapshot and BLS reference context

## Reviewer Quick Start (Simple Path)

Using the email mentioned in the test user removes the need for gmail authentication.

1. Clone and install:

```bash
git clone git@github.com:quentesia/pathway-rfd.git
cd pathway-rfd
make setup
make env
```

2. Add required keys in `.env`:
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY` (optional backup-only)
- `GMAIL_SENDER`

3. Place `credentials.json` (Google OAuth desktop client) in repo root.

4. Authenticate Gmail once:

```bash
make auth-gmail
```

5. Run app:

```bash
make run
```

6. In UI, keep `Outreach Mode = Dry Run` for safest evaluation.

Notes:
- Dry Run still uses Gmail API, but sends to Yopmail demo inboxes instead of real distributors.
- If OAuth consent screen is in Google “Testing” mode, add evaluator emails under OAuth test users.

## Engineering Highlights

For the most important architecture/tradeoff decisions (organized by pipeline step), see:

- [DECISIONS.md](./DECISIONS.md)

## Install

### 1) Clone

```bash
git clone git@github.com:quentesia/pathway-rfd.git
cd pathway-rfd
```

### 2) Create environment and install dependencies

Using `uv` (recommended):

```bash
uv sync
```

Or via Makefile:

```bash
make setup
```

Using `venv` + `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3) Configure environment variables

```bash
cp .env.example .env
```

Or via Makefile:

```bash
make env
```

Required:

- `ANTHROPIC_API_KEY` (primary LLM provider)
- `GMAIL_SENDER`

Optional:

- `OPENAI_API_KEY` (backup LLM provider used only if Anthropic fails)
- `SERPER_API_KEY` (improves distributor discovery)
- `RFP_DEMAND_TIER` (`Conservative`, `Standard`, `Busy`, `High Volume`)
- `RFP_WEEKLY_COVERS_BY_CATEGORY` (JSON category-level overrides)

## Run

### Start the UI

```bash
uv run streamlit run ui/streamlit_app.py
```

Or:

```bash
make run
```

## Gmail Setup (Required For Both Modes)

Both `Dry Run` and `Live` use Gmail API:
- `Dry Run`: sends from your Gmail to Yopmail inboxes
- `Live`: sends from your Gmail to real distributor contacts/forms

1. Create or choose a Google Cloud project.
2. Enable the Gmail API.
3. Configure OAuth consent screen.
4. Create OAuth client credentials (`Desktop app`).
5. Download the OAuth client file as `credentials.json` into repo root.
6. Set `.env` with your sender account:

```bash
GMAIL_SENDER=you@gmail.com
```

7. Authenticate once to generate `token.json`:

```bash
uv run python reauth.py
```

Or:

```bash
make auth-gmail
```

8. Start app and choose `Outreach Mode` (`Dry Run` or `Live`).

## Utilities

Reset pipeline tables while preserving BLS cache:

```bash
uv run python reset_db.py
```

Or:

```bash
make reset-db
```

Optional scheduled inbox polling helper:

```bash
uv run python poll_inbox.py
```

Or:

```bash
make poll-inbox
```

## Makefile Quickstart

```bash
make help
make setup
make env
make auth-gmail
make run
```
