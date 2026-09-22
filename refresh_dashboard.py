#!/usr/bin/env python3
"""
refresh_dashboard.py

Weekly-run script (designed for GitHub Actions) that:
  1. Checks BIS and NBIM listing pages for the newest publications.
  2. For anything not already in the local cache, downloads the real source
     document (BIS: the PDF; NBIM: the full letter/note page) and has Claude
     read the FULL text to write structured bullets: Question / Data & method /
     Findings / In numbers.
  3. Writes a single self-contained HTML page with two tabs:
       - "Dashboard": all tracked publications, same as before.
       - "What's new": a running newsletter — one entry per run in which new
         publications were found, newest first.
  4. Persists a small JSON cache of structured summaries (NOT raw PDFs) so
     re-runs never re-summarize something already processed, and never lose
     the "what's new" history.

SETUP (local test run)
-----------------------
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY="sk-ant-..."
    python refresh_dashboard.py --summarize

GITHUB ACTIONS
--------------
See .github/workflows/refresh.yml — it runs this on a weekly schedule,
commits the updated docs/index.html + JSON caches back to the repo, and
GitHub Pages serves docs/index.html as the live dashboard URL.

ADDING MORE SOURCES LATER
--------------------------
Each source is one entry in the SOURCES list. A source needs: a name, a
listing-page URL, a list_parser (pulls title/url/date out of the listing
page) and a source_reader (downloads/reads the real document's full text).
See the BIS and NBIM implementations for the two patterns already covered.
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ResearchDashboardBot/1.0; personal use)"
}


# --------------------------------------------------------------------------
# Fetching helpers
# --------------------------------------------------------------------------

def fetch_text(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def fetch_bytes(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.content


def cache_path(cache_dir: str, url: str, ext: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return os.path.join(cache_dir, f"{digest}{ext}")


def download_and_cache(url: str, cache_dir: str, ext: str) -> str:
    """Downloads url into cache_dir once (ephemeral scratch space on the
    Actions runner — not committed to git, see .gitignore). Returns the
    local path."""
    os.makedirs(cache_dir, exist_ok=True)
    path = cache_path(cache_dir, url, ext)
    if os.path.exists(path):
        return path
    data = fetch_bytes(url)
    with open(path, "wb") as f:
        f.write(data)
    return path


def extract_pdf_text(path: str, max_chars: int = 20000) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("  [warn] pypdf not installed; skipping PDF text extraction", file=sys.stderr)
        return ""
    try:
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text[:max_chars]
    except Exception as e:
        print(f"  [warn] could not read PDF {path}: {e}", file=sys.stderr)
        return ""


# --------------------------------------------------------------------------
# Site-specific listing-page parsers. Each returns [{title, url, date}, ...]
# --------------------------------------------------------------------------

def parse_bis_listing(listing_url: str, limit: int = 8) -> list:
    soup = BeautifulSoup(fetch_text(listing_url), "html.parser")
    items = []
    for a in soup.select("a[href^='/publications/']"):
        href = a.get("href", "")
        if href.rstrip("/") in ("/publications", listing_url):
            continue
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 15:
            continue
        m = re.search(r"(\d{1,2} [A-Za-z]{3} \d{4})", text)
        date_str = ""
        if m:
            try:
                date_str = datetime.strptime(m.group(1), "%d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
        items.append({
            "title": text,
            "url": "https://www.bis.org" + href if href.startswith("/") else href,
            "date": date_str,
        })
        if len(items) >= limit:
            break
    return items


def parse_nbim_listing(listing_url: str, limit: int = 8) -> list:
    soup = BeautifulSoup(fetch_text(listing_url), "html.parser")
    items = []
    for a in soup.select("a[href*='/en/news-and-insights/']"):
        href = a.get("href", "")
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 8:
            continue
        if href.rstrip("/") == listing_url.rstrip("/"):
            continue
        items.append({
            "title": title,
            "url": "https://www.nbim.no" + href if href.startswith("/") else href,
            "date": "",
        })
        if len(items) >= limit:
            break
    return items


# --------------------------------------------------------------------------
# Site-specific "get the real source document" readers
# --------------------------------------------------------------------------

def get_bis_source_text(item: dict, cache_dir: str) -> str:
    landing_html = fetch_text(item["url"])
    soup = BeautifulSoup(landing_html, "html.parser")
    pdf_link = soup.find("a", string=re.compile(r"PDF", re.I))
    if not pdf_link or not pdf_link.get("href"):
        return soup.get_text(" ", strip=True)[:6000]

    pdf_url = pdf_link["href"]
    if pdf_url.startswith("/"):
        pdf_url = "https://www.bis.org" + pdf_url

    local_pdf = download_and_cache(pdf_url, cache_dir, ".pdf")
    text = extract_pdf_text(local_pdf)
    return text or soup.get_text(" ", strip=True)[:6000]


def get_nbim_source_text(item: dict, cache_dir: str) -> str:
    local_html = download_and_cache(item["url"], cache_dir, ".html")
    with open(local_html, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()
    soup = BeautifulSoup(raw, "html.parser")
    main = soup.find("main") or soup
    return main.get_text(" ", strip=True)[:20000]


# --------------------------------------------------------------------------
# Structured AI summarization (Question / Data & method / Findings / Numbers)
# --------------------------------------------------------------------------

STRUCTURE_PROMPT = """Read the following publication text and respond with ONLY a JSON object
(no markdown fences, no preamble) with these exact keys:

{{
  "question": "one sentence: the core question this publication tries to answer",
  "method": "one sentence: the data and/or method actually used",
  "findings": ["short qualitative finding", "..."],
  "numbers": ["short finding with a specific figure/statistic", "..."],
  "blurb": "one short sentence (under 20 words) summarizing this for a newsletter reader"
}}

Rules:
- "findings": 2-4 short bullet-point sentences, qualitative (no numbers required).
- "numbers": 0-3 short bullet-point sentences, ONLY include this if the text actually
  states specific figures, percentages, or statistics. If there are none, return an empty list.
- Do not invent numbers or methods not present in the text.
- Keep every bullet under ~25 words.

Publication text:
{text}
"""

TRENDS_PROMPT = """You maintain a research dashboard tracking publications from the Bank for
International Settlements (BIS) and Norges Bank Investment Management (NBIM). Below are the
titles and key findings of everything currently on the dashboard.

Write ONLY a JSON object (no markdown fences, no preamble) with one key:
{{
  "trends": ["short trend observation naming specific publications/topics", "..."]
}}

Write 3-5 bullets, each under 35 words, each grounded in specific items below (not generic
statements). Focus on genuine cross-cutting patterns: shared topics, methodological shifts,
or themes that connect multiple publications.

Publications:
{items_text}
"""


def call_claude(prompt: str, max_tokens: int = 600) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    return re.sub(r"^```json\s*|\s*```$", "", raw.strip())


def summarize_with_claude(text: str, fallback: str) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY") or not text.strip():
        return {"question": "", "method": "", "findings": [fallback[:200]], "numbers": [], "blurb": fallback[:120]}
    try:
        parsed = json.loads(call_claude(STRUCTURE_PROMPT.format(text=text[:18000])))
        return {
            "question": parsed.get("question", ""),
            "method": parsed.get("method", ""),
            "findings": parsed.get("findings", []) or [],
            "numbers": parsed.get("numbers", []) or [],
            "blurb": parsed.get("blurb", fallback[:120]),
        }
    except Exception as e:
        print(f"  [warn] summarization failed: {e}", file=sys.stderr)
        return {"question": "", "method": "", "findings": [fallback[:200]], "numbers": [], "blurb": fallback[:120]}


def generate_trends(all_results: dict) -> list:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []
    lines = []
    for label, items in all_results.items():
        for item in items:
            s = item.get("structured", {})
            findings = "; ".join(s.get("findings", [])[:2])
            lines.append(f"- [{label}] {item['title']}: {findings}")
    if not lines:
        return []
    try:
        parsed = json.loads(call_claude(TRENDS_PROMPT.format(items_text="\n".join(lines)), max_tokens=500))
        return parsed.get("trends", []) or []
    except Exception as e:
        print(f"  [warn] trend generation failed: {e}", file=sys.stderr)
        return []


# --------------------------------------------------------------------------
# Sources to track. Add more by appending to this list.
# --------------------------------------------------------------------------

SOURCES = [
    {"group": "BIS", "label": "BIS Bulletins", "url": "https://www.bis.org/publications/bulletin",
     "list_parser": parse_bis_listing, "source_reader": get_bis_source_text},
    {"group": "BIS", "label": "BIS Working Papers", "url": "https://www.bis.org/publications/working-paper",
     "list_parser": parse_bis_listing, "source_reader": get_bis_source_text},
    {"group": "BIS", "label": "BIS Papers", "url": "https://www.bis.org/publications/bis-paper",
     "list_parser": parse_bis_listing, "source_reader": get_bis_source_text},
    {"group": "NBIM", "label": "Submissions to ministry", "url": "https://www.nbim.no/en/news-and-insights/submissions-to-ministry/",
     "list_parser": parse_nbim_listing, "source_reader": get_nbim_source_text},
    {"group": "NBIM", "label": "Discussion notes", "url": "https://www.nbim.no/en/news-and-insights/publications/discussion-notes/",
     "list_parser": parse_nbim_listing, "source_reader": get_nbim_source_text},
    {"group": "NBIM", "label": "Asset manager perspectives", "url": "https://www.nbim.no/en/news-and-insights/publications/asset-manager-perspectives/",
     "list_parser": parse_nbim_listing, "source_reader": get_nbim_source_text},
]


# --------------------------------------------------------------------------
# HTML output
# --------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Policy & Markets Briefing</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,wght@0,500;0,600;1,400&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  :root {{ --paper:#F5F2EA; --panel:#FFFFFF; --ink:#221E17; --soft:#58524A; --rule:#DAD3C3; --navy:#16243B;
           --bis:#A97C34; --bis-tint:#F4ECDC; --nbim:#2F6E63; --nbim-tint:#E7F0ED; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family:'IBM Plex Sans',system-ui,sans-serif; max-width: 960px; margin: 0 auto; padding: 0 22px 60px;
          color: var(--ink); background: var(--paper); line-height: 1.55; }}
  h1 {{ font-family:'Newsreader',Georgia,serif; font-weight: 600; font-size: 40px; margin: 34px 0 6px; }}
  .updated {{ color: var(--soft); font-size: 12.5px; font-family:'IBM Plex Mono',monospace; margin-bottom: 22px; }}
  .tabs {{ display: flex; gap: 8px; border-bottom: 3px solid var(--ink); margin-bottom: 28px; }}
  .tab-btn {{ font-family:'IBM Plex Sans',sans-serif; font-size: 14px; font-weight: 600; padding: 10px 18px;
              border: 1px solid var(--rule); border-bottom: none; background: var(--panel); color: var(--soft);
              cursor: pointer; border-radius: 4px 4px 0 0; position: relative; top: 1px; }}
  .tab-btn.active {{ background: var(--ink); color: var(--paper); border-color: var(--ink); }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  h2 {{ font-family:'Newsreader',Georgia,serif; font-size: 24px; border-bottom: 2px solid var(--ink); padding-bottom: 8px; margin-top: 40px; }}
  h3.group-label {{ font-family:'IBM Plex Mono',monospace; font-size: 11.5px; letter-spacing:.03em; display:inline-block;
                     padding: 3px 9px; margin: 22px 0 12px; }}
  h3.group-label.bis {{ background: var(--bis-tint); color: var(--bis); }}
  h3.group-label.nbim {{ background: var(--nbim-tint); color: var(--nbim); }}
  article {{ padding: 16px 0; border-top: 1px solid var(--rule); }}
  .date {{ font-family:'IBM Plex Mono',monospace; font-size: 12px; color: var(--soft); }}
  article h4 {{ font-family:'Newsreader',Georgia,serif; font-weight: 500; font-size: 18px; margin: 4px 0 10px; }}
  article h4 a {{ color: var(--ink); text-decoration: none; }}
  article h4 a:hover {{ text-decoration: underline; }}
  dl.insight {{ display: grid; grid-template-columns: 108px 1fr; gap: 6px 14px; margin: 0; max-width: 68ch; }}
  dl.insight dt {{ font-family:'IBM Plex Mono',monospace; font-size: 10.5px; color: var(--soft); padding-top: 2px; }}
  dl.insight dd {{ margin: 0; font-size: 14.5px; }}
  dl.insight dd ul {{ margin: 0; padding-left: 16px; }}
  dl.insight dd ul li {{ margin-bottom: 3px; }}
  .trends {{ background: var(--panel); border: 1px solid var(--rule); border-left: 4px solid var(--navy); padding: 18px 22px; margin: 24px 0; }}
  .trends h2 {{ font-size: 13px; font-family:'IBM Plex Mono',monospace; border: none; margin: 0 0 10px; color: var(--soft); text-transform: none; }}
  .trends ul {{ margin: 0; padding-left: 18px; }}
  .trends li {{ font-size: 15px; margin-bottom: 8px; }}
  .pub-count {{ font-size: 13.5px; color: var(--soft); border-top: 1px dashed var(--rule); padding-top: 12px; margin-top: 14px; }}
  .pub-count strong {{ font-family:'IBM Plex Mono',monospace; color: var(--ink); }}
  .newsletter-entry {{ border-top: 2px solid var(--ink); padding: 18px 0; }}
  .newsletter-entry .run-date {{ font-family:'Newsreader',Georgia,serif; font-style: italic; font-size: 19px; margin-bottom: 10px; }}
  .newsletter-item {{ padding: 8px 0 8px 14px; border-left: 3px solid var(--rule); margin-bottom: 8px; }}
  .newsletter-item a {{ font-weight: 600; color: var(--ink); text-decoration: none; }}
  .newsletter-item a:hover {{ text-decoration: underline; }}
  .newsletter-item .src-tag {{ font-family:'IBM Plex Mono',monospace; font-size: 10px; color: var(--soft); margin-right: 6px; }}
  .newsletter-item p {{ margin: 4px 0 0; font-size: 14px; }}
  .empty-note {{ color: var(--soft); font-size: 14px; font-style: italic; }}
</style>
</head>
<body>
  <h1>Policy & Markets Briefing</h1>
  <div class="updated">Last refreshed: {updated}</div>

  <div class="tabs">
    <button class="tab-btn active" onclick="showTab('dashboard')">Dashboard</button>
    <button class="tab-btn" onclick="showTab('whats-new')">What's new</button>
  </div>

  <div id="dashboard" class="tab-panel active">
    {trends_block}
    {dashboard_body}
  </div>

  <div id="whats-new" class="tab-panel">
    {newsletter_body}
  </div>

<script>
function showTab(id) {{
  document.querySelectorAll('.tab-panel').forEach(function(p){{ p.classList.remove('active'); }});
  document.querySelectorAll('.tab-btn').forEach(function(b){{ b.classList.remove('active'); }});
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}
</script>
</body>
</html>
"""

GROUP_TEMPLATE = "<h2>{group_label}</h2>\n{sections}"
SECTION_TEMPLATE = "<h3 class=\"group-label {css}\">{label}</h3>\n{items}"

ITEM_TEMPLATE = """<article>
  <div class="date">{date}</div>
  <h4><a href="{url}" target="_blank" rel="noopener">{title}</a></h4>
  <dl class="insight">
    {question_block}
    {method_block}
    <dt>Findings</dt><dd><ul>{findings_html}</ul></dd>
    {numbers_block}
  </dl>
</article>"""


def render_item(item: dict) -> str:
    s = item.get("structured", {})
    question_block = f"<dt>Question</dt><dd>{html.escape(s['question'])}</dd>" if s.get("question") else ""
    method_block = f"<dt>Data &amp; method</dt><dd>{html.escape(s['method'])}</dd>" if s.get("method") else ""
    findings_html = "".join(f"<li>{html.escape(f)}</li>" for f in s.get("findings", []))
    numbers = s.get("numbers", [])
    numbers_block = (
        f"<dt>In numbers</dt><dd><ul>{''.join(f'<li>{html.escape(n)}</li>' for n in numbers)}</ul></dd>"
        if numbers else ""
    )
    return ITEM_TEMPLATE.format(
        date=item.get("date") or "",
        url=html.escape(item["url"]),
        title=html.escape(item["title"]),
        question_block=question_block,
        method_block=method_block,
        findings_html=findings_html or "<li>(no summary available)</li>",
        numbers_block=numbers_block,
    )


def build_dashboard_body(all_results: dict) -> str:
    groups_order = []
    for s in SOURCES:
        if s["group"] not in groups_order:
            groups_order.append(s["group"])

    body_parts = []
    for group in groups_order:
        sections = []
        for s in SOURCES:
            if s["group"] != group:
                continue
            items_html = "\n".join(render_item(item) for item in all_results[s["label"]]) or "<p class='empty-note'>No items found.</p>"
            css = "bis" if group == "BIS" else "nbim"
            sections.append(SECTION_TEMPLATE.format(css=css, label=html.escape(s["label"]), items=items_html))
        body_parts.append(GROUP_TEMPLATE.format(group_label=html.escape(group), sections="\n".join(sections)))
    return "\n".join(body_parts)


def build_trends_block(all_results: dict, trends: list) -> str:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    by_group = {}
    total = 0
    for s in SOURCES:
        for item in all_results.get(s["label"], []):
            d = item.get("date")
            if not d:
                continue
            try:
                dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if dt >= cutoff:
                by_group[s["group"]] = by_group.get(s["group"], 0) + 1
                total += 1

    count_line = f"<strong>{total}</strong> new publications in the past 30 days" if total else "No new publications in the past 30 days"
    if by_group:
        parts = ", ".join(f"<strong>{n}</strong> from {g}" for g, n in by_group.items())
        count_line += f" — {parts}."
    else:
        count_line += "."

    trends_html = "".join(f"<li>{html.escape(t)}</li>" for t in trends)
    trends_section = f"<ul>{trends_html}</ul>" if trends_html else "<p class='empty-note'>Trend summary needs ANTHROPIC_API_KEY set.</p>"

    return f"""<div class="trends">
      <h2>Across sources this cycle</h2>
      {trends_section}
      <div class="pub-count">{count_line}</div>
    </div>"""


def build_newsletter_body(newsletter_history: list) -> str:
    if not newsletter_history:
        return "<p class='empty-note'>No runs recorded yet.</p>"

    entries = []
    for entry in newsletter_history:
        items_html = ""
        for it in entry["items"]:
            items_html += f"""<div class="newsletter-item">
              <span class="src-tag">{html.escape(it['group'])} · {html.escape(it['label'])}</span><br>
              <a href="{html.escape(it['url'])}" target="_blank" rel="noopener">{html.escape(it['title'])}</a>
              <p>{html.escape(it.get('blurb',''))}</p>
            </div>"""
        entries.append(f"""<div class="newsletter-entry">
          <div class="run-date">{html.escape(entry['run_date'])} — {len(entry['items'])} new</div>
          {items_html}
        </div>""")
    return "\n".join(entries)


def build_html(all_results: dict, trends: list, newsletter_history: list) -> str:
    return PAGE_TEMPLATE.format(
        updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        trends_block=build_trends_block(all_results, trends),
        dashboard_body=build_dashboard_body(all_results),
        newsletter_body=build_newsletter_body(newsletter_history),
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/index.html", help="Output HTML file path")
    ap.add_argument("--limit", type=int, default=8, help="Max items per source")
    ap.add_argument("--summarize", action="store_true", help="Have Claude read full sources and write structured bullets + trends (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--cache-dir", default="/tmp/pdf_cache", help="Ephemeral scratch folder for downloaded PDFs/pages (not committed to git)")
    ap.add_argument("--json-cache", default="dashboard_cache.json", help="Persisted structured-summary cache (committed to git)")
    ap.add_argument("--newsletter-cache", default="newsletter_history.json", help="Persisted newsletter history (committed to git)")
    args = ap.parse_args()

    previous = {}
    if os.path.exists(args.json_cache):
        with open(args.json_cache) as f:
            try:
                previous = {item["url"]: item for group in json.load(f).values() for item in group}
            except Exception:
                previous = {}
    is_first_run = not previous

    newsletter_history = []
    if os.path.exists(args.newsletter_cache):
        with open(args.newsletter_cache) as f:
            try:
                newsletter_history = json.load(f)
            except Exception:
                newsletter_history = []

    all_results = {}
    new_items_this_run = []

    for s in SOURCES:
        print(f"Fetching {s['label']} ...")
        try:
            items = s["list_parser"](s["url"], limit=args.limit)
        except Exception as e:
            print(f"  [error] could not fetch {s['label']}: {e}", file=sys.stderr)
            items = []

        for item in items:
            is_new = item["url"] not in previous

            if not is_new:
                item["structured"] = previous[item["url"]].get("structured", {})
                all_results.setdefault(s["label"], []).append(item)
                continue

            if args.summarize:
                print(f"  New item — reading full source: {item['title'][:60]}...")
                try:
                    text = s["source_reader"](item, args.cache_dir)
                except Exception as e:
                    print(f"  [warn] could not read source for {item['url']}: {e}", file=sys.stderr)
                    text = ""
                item["structured"] = summarize_with_claude(text, item["title"])
            else:
                item["structured"] = {"question": "", "method": "", "findings": [item["title"]], "numbers": [], "blurb": item["title"]}

            all_results.setdefault(s["label"], []).append(item)

            if not is_first_run:
                new_items_this_run.append({
                    "group": s["group"],
                    "label": s["label"],
                    "title": item["title"],
                    "url": item["url"],
                    "blurb": item["structured"].get("blurb", ""),
                })

    # Make sure every configured source has an (possibly empty) entry
    for s in SOURCES:
        all_results.setdefault(s["label"], [])

    if new_items_this_run:
        newsletter_history.insert(0, {
            "run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "items": new_items_this_run,
        })
        newsletter_history = newsletter_history[:26]  # keep roughly six months of weekly runs
    elif is_first_run:
        newsletter_history.insert(0, {
            "run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "items": [{"group": "—", "label": "—", "title": "Initial dashboard build", "url": "", "blurb": f"{sum(len(v) for v in all_results.values())} publications loaded."}],
        })

    trends = generate_trends(all_results) if args.summarize else []

    with open(args.json_cache, "w") as f:
        json.dump(all_results, f, indent=2)
    with open(args.newsletter_cache, "w") as f:
        json.dump(newsletter_history, f, indent=2)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        f.write(build_html(all_results, trends, newsletter_history))

    print(f"\nDone. Wrote {args.out}")
    print(f"New items this run: {len(new_items_this_run)}")


if __name__ == "__main__":
    main()
