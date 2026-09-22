# Policy & Markets Briefing

An automatically-refreshing dashboard tracking new publications from:

- **BIS** — Bulletins, Working Papers, BIS Papers
- **NBIM** (Government Pension Fund Global's manager) — Submissions to ministry,
  Discussion notes, Asset manager perspectives

Every week, a GitHub Action checks each source for new publications, has
Claude read the *full* source document (not just a listing-page teaser) and
write structured bullets — Question / Data & method / Findings / In numbers —
and rebuilds the dashboard. Anything new since the last run also lands as an
entry on the **What's new** tab, so the dashboard doubles as a lightweight
newsletter with no separate delivery channel to manage.

## One-time setup

1. **Create the repo.** Create a new GitHub repository and add all the files
   in this folder to it (commit and push).

2. **Add your Anthropic API key as a secret.**
   Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `ANTHROPIC_API_KEY`
   - Value: your key from [console.anthropic.com](https://console.anthropic.com)

   Without this, the workflow still runs and updates the list of publications,
   but falls back to bare titles instead of the structured bullets and skips
   the trends box.

3. **Turn on GitHub Pages.**
   Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch:
   `main`, folder: **/docs** → Save.
   Your dashboard will then be live at:
   `https://<your-username>.github.io/<repo-name>/`
   (GitHub shows the exact URL on that same Pages settings screen once it's built.)

4. **Run it once manually** to build the first version instead of waiting for
   Monday. Repo → **Actions** tab → **Refresh dashboard** (left sidebar) →
   **Run workflow** button → **Run workflow**. It takes a few minutes,
   mostly spent reading source PDFs. Refresh the Pages URL once it's done.

That's it — after this it refreshes itself every Monday at 07:00 UTC.

## How it decides what's "new"

`dashboard_cache.json` stores one entry per publication URL, including its
structured summary. Each run only downloads and summarizes URLs it hasn't
seen before; everything else is reused from the cache untouched. Whatever's
genuinely new in a given run becomes that run's entry in
`newsletter_history.json`, shown on the **What's new** tab.

Both JSON files are committed back to the repo by the workflow, so the
history is permanent and versioned — you can see exactly what was added and
when by looking at the git log for those two files.

Note that raw downloaded PDFs themselves are **not** kept — only the
structured summary Claude produces from them. That's enough to never need to
re-fetch or re-read a publication once it's been processed, without bloating
the repo with binary files.

## Running it locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
python refresh_dashboard.py --summarize
```

Useful flags:

- `--out PATH` — where to write the HTML (default `docs/index.html`)
- `--limit N` — how many recent items to check per source (default 8)
- `--cache-dir DIR` — scratch folder for downloaded PDFs (default `/tmp/pdf_cache`)

## Changing the schedule

Edit the `cron` line in `.github/workflows/refresh.yml`. Cron time is always
UTC. Some examples:

- Daily at 07:00 UTC: `0 7 * * *`
- Twice a week (Mon & Thu): `0 7 * * 1,4`
- Monthly, 1st of the month: `0 7 1 * *`

You can always trigger an extra run anytime from the Actions tab regardless
of the schedule.

## Adding more sources

Each source is one entry in the `SOURCES` list in `refresh_dashboard.py`. A
source needs a listing-page URL, a `list_parser` (pulls title/url/date out of
the listing page's HTML) and a `source_reader` (downloads and extracts the
full text of one publication). The BIS and NBIM implementations cover two
common patterns — a PDF-per-item site, and a site where the full text is on
the HTML page itself — most other research/publication sites will fit one of
the two with only the CSS selectors changed.
