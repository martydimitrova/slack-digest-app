# Slack Digest App

Scrapes your Slack sidebar into a structured snapshot, lets you configure which sections to summarise and at what depth, generates AI-powered digests, and posts a summary to a Slack channel of your choice.

> This tool is designed for local single-user use and performs no input sanitisation. Do not expose it on a network.

## Setup

```bash
cd slack-digest-app
pip install -r requirements.txt
playwright install chromium
```

### Environment file

Create `.env` at the project root with the following keys:

```
ANTHROPIC_API_KEY=sk-ant-...             # required for digest generation and AI diff summaries
SLACK_DIGEST_CHANNEL=my-digest-channel   # channel to post the digest summary to (without the #)
```

The server loads `.env` automatically on startup. It is gitignored.

## First-time scrape

Run the scraper once from the terminal to authenticate with Slack and populate `data/sidebar.json` with your sidebar sections and channels:

```bash
python scraper.py --workspace TXXXXXXXXXX
```

A Chromium window opens. Log in to Slack if prompted (enterprise SSO is handled automatically). The browser stays open while sections and channels are harvested, then closes.

Once the scrape completes, start the server and open `/configs` to create your first config.

To force a fresh login session:

```bash
python scraper.py --workspace TXXXXXXXXXX --reset-session
```

## Running the server

```bash
python server.py
```

Opens `http://localhost:8080` in your browser automatically.

| URL | Purpose |
|-----|---------|
| `/` | Digest library — list of all runs + Generate Digest button |
| `/digests/<config_name>/<run_id>` | A specific digest |
| `/configs` | List and create named config profiles |
| `/config/<name>` | Edit section settings for a config profile, trigger re-scrapes |

Custom port:

```bash
python server.py --port 9090
```

## Configuring sections (`/configs` and `/config/<name>`)

`/configs` lists all named config profiles and lets you create new ones. Open a profile to edit it — each Slack sidebar section appears as a card. Per section you can set:

- **Depth** — `skip` (exclude), `low`, `medium`, or `high`
- **Focus prompt** — free-text framing for the AI summariser (e.g. "focus on incidents")
- **Channels** — checkboxes to include/exclude individual channels

New sections default to `skip`. Save with the **Save** button.

### Refreshing from Slack

Click **↻ Refresh from Slack** to re-run the scraper from the UI. A progress modal streams the output. Keep the Chromium window open until it finishes.

After a successful re-scrape, an AI diff panel appears summarising what changed. Dismiss it with **×** once reviewed.

## Generating a digest (`/`)

Select a time window (default: last 24 hours) and click **▶ Generate Digest**. A progress modal streams Claude's output as it reads channels and builds the digest. When done, the new digest appears in the library and a 3–5 bullet summary is posted to your configured Slack channel (requires `SLACK_DIGEST_CHANNEL` in `.env`).

The prompt Claude uses is in `digest_prompt.txt` — edit it freely to tune the output.

### Depth behaviour

| Depth | Output |
|-------|--------|
| `high` | All channels; all notable threads summarised individually |
| `medium` | All channels; a few aggregated conversations each; surfaces dates/deadlines |
| `low` | Single paragraph across all channels in the section combined |
| `skip` | Section excluded from digest |

## File layout

```
slack-digest-app/
├── scraper.py           # Playwright scraper — writes data/sidebar.json
├── server.py            # Flask web server
├── config.py            # Config load/save helpers
├── diff.py              # Sidebar diff + Claude AI analysis
├── digest_prompt.txt    # Editable prompt template for digest generation
├── templates/           # Jinja2 HTML templates
├── tests/               # pytest tests
├── requirements.txt
├── .gitignore
├── .claude/
│   └── settings.local.json  # Grants the digest subprocess permission to call Slack MCP tools
│
└── data/                # Runtime state — fully gitignored
    ├── sidebar.json          # Current scraped Slack snapshot
    ├── sidebar.prev.json     # Previous scrape snapshot (used for diff)
    ├── scrape.log            # Scraper output log
    ├── scrape.status         # Scraper real-time status (streamed to UI)
    ├── digest.log            # Digest generation log
    ├── digest.status         # Digest generation status (streamed to UI)
    ├── configs/
    │   └── <name>/
    │       ├── config.json       # Section settings for this profile
    │       └── config.prev.json  # Previous config (used for diff)
    ├── digests/
    │   └── <name>/
    │       └── <window_start>--<window_end>.json  # Digest files
    └── .playwright-session/  # Saved Slack browser login
```

## Running tests

```bash
pytest tests/
```
