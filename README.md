# Website change monitor

For product, pricing and marketing teams who keep an eye on other sites: when a competitor's pricing, plans, changelog or terms page really changes, a GitHub issue with the diff opens in your repo and Slack, Discord or any webhook gets a ping with the link, at the next check, every 6 hours by default.

[![CI](https://github.com/ScrapeUnblocker/website-change-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/ScrapeUnblocker/website-change-monitor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Powered by ScrapeUnblocker](https://img.shields.io/badge/powered%20by-ScrapeUnblocker-6c47ff.svg)](https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos)

It runs on a GitHub Actions schedule in your own copy of this repo. Nothing to host. Pages are read as clean Markdown, so the diff shows text that changed, not markup, tracking links or rotating class names. You only hear about it when something changed.

## What actually arrives

This is the real message from a test run against github.com/pricing. To make the test show a change, the stored copy of the page was first edited back to older prices ($3 and $19) with one feature line removed. Everything on the `+` side came from the live page.

```text
GitHub pricing changed
https://github.com/pricing
+3 / -2 lines · prices $3 → $4, $19 → $21

@@ ## Team @@
 Advanced collaboration for individuals and organizations
-$3USDper user / month
+$4USDper user / month
 Continue with Team
@@ ### Everything from Free, plus: @@
 - Multiple reviewers in pull requests
+- Draft pull requests
 - Code owners
@@ ## Enterprise @@
 AI-powered development, governed at enterprise scale
-$19USDper user / month
+$21USDper user / month
 Start a free trial Contact Sales

Full diff: https://github.com/<you>/<your-repo>/issues/1
Compared with the version from 2026-11-03 06:17 UTC.
```

(In the test the issue link pointed at a local test server. In your copy it points at your repo.)

- **GitHub Issues** gets one issue per change, titled `GitHub pricing changed: $3 → $4, $19 → $21` and labelled `page-change`, with the full diff in a `diff` block. Close it once you have looked.
- **Slack and Discord** get the same message with a bold, linked title (Discord colours the diff). Long diffs are cut to `diff_lines`; the issue always has all of it.
- **The webhook** gets that text as `text`, plus the fields as JSON: `added_lines`, `removed_lines`, `prices` (`[{"before": "$3", "after": "$4"}, ...]`), `diff`, `issue_url` and the page.

The first run sends one "now watching" message to the chat destinations so you know the setup works:

```text
Now watching 2 pages. Changes will be posted here with a diff.
- GitHub pricing (https://github.com/pricing): 482 lines, prices on the page: $0, $4, $21, $0.18, $0.07, $5
- Linear pricing (https://linear.app/pricing): 341 lines, prices on the page: $0, $10, $16
```

If a page cannot be read on 3 checks in a row (moved, blocked, a `section` heading renamed), you get one "Can't read" alert instead of silence.

## Setup in three steps

1. **Use this template.** Click **Use this template** > **Create a new repository**. Make it **private**: that keeps your watchlist to yourself, and GitHub pauses scheduled workflows in public repos after 60 days without a commit (this repo only commits when a page changes).
2. **Add secrets.** In your new repo go to **Settings > Secrets and variables > Actions** and add:
   - `SCRAPEUNBLOCKER_KEY`: your API key from [ScrapeUnblocker](https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
   - any chat destination you want (optional, GitHub Issues work without one):

   | Destination | Secret | Where to get it |
   | - | - | - |
   | GitHub Issues | none | The workflow uses its own token (`issues: write` is already set in `run.yml`) |
   | Slack | `SLACK_WEBHOOK_URL` | [Create an incoming webhook](https://api.slack.com/messaging/webhooks) for a channel |
   | Discord | `DISCORD_WEBHOOK_URL` | Channel settings > Integrations > Webhooks > New Webhook > Copy Webhook URL |
   | Webhook | `ALERT_WEBHOOK_URL` | Any URL that accepts a JSON POST: Zapier, Make, n8n or your own endpoint |

3. **Edit `config.yml`.** Replace the example pages with the ones you care about (a `name` and a `url` each) and commit.

The workflow then checks every 6 hours. To start right away, open **Actions > Check pages > Run workflow**. Tick **Dry run** to see what would be sent without sending it. The first run only records each page and posts the "now watching" message; changes are reported from the second run on.

Until `SCRAPEUNBLOCKER_KEY` is set, the workflow prints a notice and exits green without doing anything.

### Run it on your own machine instead

```bash
git clone https://github.com/<you>/website-change-monitor && cd website-change-monitor
python -m venv .venv && . .venv/bin/activate
pip install .
cp .env.example .env              # fill in SCRAPEUNBLOCKER_KEY and a destination
website-change-monitor preview github.com-pricing   # see the exact text that is compared
website-change-monitor run --dry-run
website-change-monitor run
```

Then schedule it with cron (`crontab -e`). This line checks every 6 hours:

```cron
17 */6 * * * cd /path/to/website-change-monitor && .venv/bin/website-change-monitor run >> changes.log 2>&1
```

The CLI reads `.env` from the working directory, and environment variables win over it. GitHub Issues from your own machine need `GITHUB_TOKEN` (a fine-grained token with **Issues: read and write** on the repo) and `GITHUB_REPOSITORY` (`owner/name`). In GitHub Actions both are set for you.

Or with Docker, no local Python needed:

```bash
docker run --rm -v "$PWD":/app -w /app --env-file .env python:3.13-slim \
  sh -c "pip install -q . && website-change-monitor run"
```

## How it works

1. **Collect.** One call per page to the ScrapeUnblocker [getPageSource API with Markdown output](https://docs.scrapeunblocker.com/guides/markdown?utm_source=github&utm_medium=integration&utm_campaign=example-repos), through the official Python SDK: `client.get_markdown(url, main_content_only=True, proxy_country="us")`. The page is rendered in a real browser, so prices that load with JavaScript are there, and it comes back as Markdown with only the main content (no menus, header or footer). A timed-out or blocked call is retried twice with a pause. A page that still fails, answers 404, or returns a bot check instead of the page is logged and skipped; its stored version is left alone.
2. **Process.** The Markdown is made stable before anything is compared:
   - **links and images are reduced to their text.** Their URLs carry tracking and experiment parameters (`?ref_cta=...`) that change between visits.
   - **a fixed country** (`country: us`). Pricing pages show local currency; with no fixed country, a page can come back in € on one run and $ on the next and every flip would look like a change. In testing, notion.com/pricing did exactly that until the country was pinned.
   - **your filters:** keep only one `section` (a heading and everything under it), drop `skip_sections`, and drop lines matching `ignore` patterns (counters like "Trusted by 50,000 companies").
   - Then a line diff against the stored version, with the nearest heading above each hunk (`@@ ## Team @@`), and a **price summary**: money amounts that changed on the same lines are paired (`$3 → $4`), others are listed as `new $X` or `removed $X`, and an amount that only moved is not reported.
   - **Guards against false alarms:** a change smaller than `min_changed_chars` is not sent (it still adds up against the stored version until it is big enough). A page whose text shrank by more than half is only believed when the next run sees the same, because error pages and half-loaded pages are short. Set `confirm_runs: 2` for a page that A/B tests its layout, so a variant has to show up twice in a row.
3. **Deliver.** GitHub Issues goes first, so the chat messages can link to the issue. Each destination gets its own format (Slack mrkdwn, Discord markdown with diff colouring, plain text plus JSON for the webhook). If no destination accepts a change, the page keeps its old version, so the next run sends it again. One failing destination does not block the others, but the run exits non-zero so the Actions run shows red.
4. **Save state.** `state/pages.json` holds, per page, a fingerprint of the compared text, when it was first seen and last changed, and how many changes were sent. `state/pages/<id>.md` holds the compared text itself. The workflow commits `state/` back (`Update watched pages [skip ci]`) only when something changed, so **the repo's git history is the full history of every page**: `git log -p state/pages/github.com-pricing.md`. A run with nothing new sends nothing and leaves every file as it was.

Three commands help while you tune the config: `website-change-monitor preview <page id, name or URL>` fetches a page (1 API call) and prints exactly the text that is compared, or the headings on the page when `section` does not match; `status` prints what is stored per page; `test-destinations` posts a "connected" message to each chat destination and checks GitHub access without opening an issue. The last two make no API calls.

## Config reference

Everything lives in [`config.yml`](config.yml). Secrets never go there.

| Key | Default | Meaning |
| - | - | - |
| `pages[].url` | (required) | The page to watch, a full `https://` URL |
| `pages[].name` | from the URL | Label used in messages and issue titles |
| `pages[].id` | from the URL, e.g. `github.com-pricing` | File name in `state/pages/`. Changing it starts the page over with a new baseline |
| `pages[].section` | none | Compare only the heading that contains this text (case-insensitive) and everything under it, up to the next heading of the same level |
| `pages[].skip_sections` | none | Headings whose sections are left out, same matching as `section` |
| `defaults.country` | `us` | Two-letter country the page is fetched from. `null` lets the API pick one (expect currency flips on pricing pages) |
| `defaults.main_content_only` | `true` | `false` compares the whole page, menus and footer included |
| `defaults.keep_links` | `false` | `true` compares link URLs too |
| `defaults.ignore` | `[]` | Regular expressions; a line that matches anywhere is dropped. A page's own `ignore` list adds to this one |
| `defaults.min_changed_chars` | `1` | Smallest change, in changed characters, that is sent |
| `defaults.confirm_runs` | `1` | Runs in a row a new version must be seen before it is sent (1-10) |
| `defaults.alert_after_failures` | `3` | Send one "Can't read" alert after this many failed checks in a row; `0` turns it off |
| `delivery.destinations` | all four | Any of `github_issues`, `slack`, `discord`, `webhook`. A destination without its secret is skipped |
| `delivery.diff_lines` | `30` | Diff lines shown in a chat message (the issue always has the full diff) |
| `delivery.baseline_message` | `true` | Post the "now watching" message when a page is checked for the first time |
| `delivery.issue_labels` | `[page-change]` | Labels for new issues; GitHub creates a missing one |
| `state_dir` | `state` | Where page versions are kept, relative to `config.yml` |

Every key under `defaults` can also be set on a single page. The schedule is in [`.github/workflows/run.yml`](.github/workflows/run.yml) (`cron: "17 */6 * * *"`, UTC). Unknown keys and invalid values stop the run with a clear error, so a typo cannot fail silently.

## Cost

Each run makes **one API request per page**. `--dry-run` makes the same requests as a real run.

With the default config (4 pages, every 6 hours), that is **4 requests per run, 16 per day, about 480 per 30 days**. Retries after a timed-out or blocked call can add a few on a bad day. `preview` makes one request; `status` and `test-destinations` make none. To spend less, check less often (for example `cron: "17 7 * * *"` for once a day) or watch fewer pages.

## Adding another destination

Destinations live behind one small interface in [`src/website_change_monitor/sinks.py`](src/website_change_monitor/sinks.py). A sink has a `name`, `accepts(event)`, `send(events)`, `preview(events)` and `test()`, and raises `DeliveryError` on failure. For a service that takes text over a webhook, subclass `TextSink`. Here is Google Chat:

```python
from website_change_monitor import messages
from website_change_monitor.sinks import REGISTRY, SinkSpec, TextSink


class GoogleChatSink(TextSink):
    name = "google_chat"
    style = messages.PLAIN
    limit = 4000

    def payload(self, text):
        return {"text": text}


REGISTRY["google_chat"] = SinkSpec(
    env=("GOOGLE_CHAT_WEBHOOK_URL",),
    build=lambda env, http, delivery: GoogleChatSink(
        env["GOOGLE_CHAT_WEBHOOK_URL"], http, delivery.diff_lines
    ),
)
```

Then add `"google_chat"` to `DESTINATIONS` in `config.py`, list it under `delivery.destinations` in `config.yml`, and pass the secret to the run step in `run.yml`. For anything else (a database, a ticket system), implement `send(events)` and use each event's fields (`event.to_dict()`): `kind` is `baseline`, `change` or `failing`.

## Development

```bash
make install    # pip install -e ".[dev]"
make lint       # ruff check + ruff format --check
make test       # offline tests: the SDK, GitHub and every destination are mocked, no API credit used
make dry-run    # real pages, nothing sent
```

The tests cover the Markdown clean-up (links, escapes, sections, ignore patterns), the diff and price pairing, the false-alarm guards (threshold, confirmation, shrink check), failure alerts, two runs in a row delivering only what is new, delivery rollback when every destination fails, each destination's message format and payload, config validation and the CLI. CI runs them on Python 3.11 and 3.13.

## Links

- [ScrapeUnblocker](https://scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos): the scraping API behind this repo
- [Documentation](https://docs.scrapeunblocker.com/?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
- [Markdown output](https://docs.scrapeunblocker.com/guides/markdown?utm_source=github&utm_medium=integration&utm_campaign=example-repos): how getPageSource turns a page into Markdown
- [Python SDK](https://docs.scrapeunblocker.com/sdks/python?utm_source=github&utm_medium=integration&utm_campaign=example-repos)
- Want whole sites as Markdown for an LLM or a RAG index instead of change alerts? [website-to-markdown](https://github.com/ScrapeUnblocker/website-to-markdown) crawls a site into Markdown files, `llms.txt` and chunks.

## License

[MIT](LICENSE). Copyright (c) 2026 ScrapeUnblocker.
