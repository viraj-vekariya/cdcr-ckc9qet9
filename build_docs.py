#!/usr/bin/env python3
"""Builds the placement+internship documents + website data from
notices_cache.json:
  2. PLACEMENT_ONLY      -- type == PLACEMENT
  3. INTERNSHIP_ONLY     -- type == INTERNSHIP
plus docs/notices.json (both categories, each row carries its `subject` so
the website can filter within a category -- e.g. exclude PPO -- without
needing a separate static PPO-excluded document). PDFs are rendered with
Playwright's bundled Chromium (page.pdf()), which works identically on
macOS and the Ubuntu CI runner.
"""
import html, json, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
CACHE = BASE / "notices_cache.json"
DOCS = BASE / "docs"
DOCS.mkdir(exist_ok=True)

CSS = """
body{font:14px/1.5 -apple-system,Helvetica,Arial,sans-serif;color:#1a1a1a;margin:0;padding:28px 34px;background:#fff}
h1{font-size:19px;margin:0 0 4px}
.meta{color:#666;font-size:12px;margin-bottom:18px}
.row{border:1px solid #ddd;border-radius:8px;padding:12px 14px;margin-bottom:10px;page-break-inside:avoid}
.hd{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;color:#444;margin-bottom:6px}
.tag{font-weight:700;padding:1px 7px;border-radius:5px;font-size:11px;text-transform:uppercase}
.tag.PLACEMENT{background:#e6f7ee;color:#0b7a3e}
.tag.INTERNSHIP{background:#eaf0ff;color:#1a4fd6}
.co{font-weight:700;font-size:13.5px}
.subj{color:#7a4b00;font-weight:600}
.body{white-space:pre-wrap;font-size:13px;color:#222;margin-top:6px}
.dl{margin-top:8px;font-size:12px;color:#0b5fff}
.idx{color:#999;font-size:11px}
"""

def esc(s): return html.escape(str(s or ""))

def load():
    if not CACHE.exists():
        sys.exit(f"No cache found at {CACHE}. Run fetch_and_cache.py first.")
    return json.loads(CACHE.read_text())

def row_html(r, idx, show_download=False):
    tag = r["type"] if r["type"] in ("PLACEMENT", "INTERNSHIP") else "OTHER"
    dl = ""
    if show_download:
        has_file = bool((r.get("download_raw") or "").strip())
        dl = (f'<div class="dl">📎 A file is attached to this notice on the ERP -- open it via the '
              f'"Download" button on the CDC Notice board (id {esc(r["id"])}).</div>') if has_file else \
             '<div class="dl">📎 No file attached to this notice.</div>'
    return f'''<div class="row">
      <div class="hd"><span class="idx">#{esc(idx)} · id {esc(r["id"])}</span>
        <span><span class="tag {tag}">{esc(r["type"])}</span> <span class="subj">{esc(r["subject"])}</span></span>
        <span>{esc(r["noticeat"])}</span></div>
      <div class="co">{esc(r["company"]) or "(no company / general notice)"}</div>
      <div class="body">{esc(r["notice"])}</div>
      {dl}
    </div>'''

def extract_download_link(raw_html):
    """Pull a href or onclick(id) pattern out of the raw download cell, if any."""
    if not raw_html:
        return None
    m = re.search(r'href=[\'"]([^\'"]+)[\'"]', raw_html)
    if m and m.group(1) not in ("#", "javascript:void(0)"):
        return m.group(1)
    m = re.search(r'onclick=[\'"]?\s*(\w+)\(([^)]*)\)', raw_html)
    if m:
        return f"{m.group(1)}({m.group(2)})  [JS action -- see notes]"
    return None

def page(title, subtitle, rows_html):
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{esc(title)}</title>
<style>{CSS}</style></head><body>
<h1>{esc(title)}</h1><div class="meta">{esc(subtitle)}</div>
{rows_html}
</body></html>"""

def write_and_pdf(browser_page, name, title, subtitle, rows, show_download=False):
    body = "\n".join(row_html(r, i+1, show_download) for i, r in enumerate(rows)) or "<p>No matching notices.</p>"
    html_path = DOCS / f"{name}.html"
    pdf_path = DOCS / f"{name}.pdf"
    html_path.write_text(page(title, subtitle, body))
    browser_page.goto(f"file://{html_path}")
    browser_page.pdf(path=str(pdf_path))
    print(f"  wrote {pdf_path.name}  ({len(rows)} rows)")

def write_json(rows):
    """Writes docs/notices.json -- the data source for the website dashboard.
    Both PLACEMENT and INTERNSHIP notices are included (per 21 Sep 2026
    request: website shows everything, WhatsApp stays placement-only). Each
    slim row keeps its `subject` so the site can filter within a category
    (e.g. hide PPO) client-side instead of needing a separate static
    PPO-excluded document. A notice gets a `file` path when its ERP
    attachment was actually captured into docs/files/ (see attachments.py);
    `has_download` alone only means the ERP lists a Download link for it."""
    import datetime
    def key(r): return int(r["id"]) if str(r["id"]).isdigit() else 0
    def slim(r):
        out = {"id": r["id"], "type": r["type"], "subject": r["subject"],
               "company": r["company"], "notice": r["notice"], "noticeat": r["noticeat"],
               "text_source": r.get("text_source", "grid"), "source_hash": r.get("source_hash", ""),
               "has_download": bool((r.get("download_raw") or "").strip())}
        f = DOCS / "files" / f"{r['id']}.pdf"
        if f.exists() and f.stat().st_size > 0:
            out["file"] = f"files/{r['id']}.pdf"
        return out

    placement = sorted([r for r in rows if r["type"] == "PLACEMENT"], key=key, reverse=True)
    internship = sorted([r for r in rows if r["type"] == "INTERNSHIP"], key=key, reverse=True)
    p_slim = [slim(r) for r in placement]
    i_slim = [slim(r) for r in internship]
    all_slim = p_slim + i_slim
    data = {
        "last_updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": {
            "placement": len(placement), "internship": len(internship),
            "with_files": sum(1 for x in all_slim if "file" in x),
            "listed_downloads": sum(1 for x in all_slim if x["has_download"]),
        },
        "categories": {
            "placement": p_slim,
            "internship": i_slim,
        },
    }
    (DOCS / "notices.json").write_text(json.dumps(data, indent=1))
    print(f"  wrote notices.json ({len(placement)} placement, {len(internship)} internship rows)")

def main():
    rows = load()
    print(f"loaded {len(rows)} cached rows\n")

    placement = [r for r in rows if r["type"] == "PLACEMENT"]
    internship = [r for r in rows if r["type"] == "INTERNSHIP"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        browser_page = browser.new_page()
        write_and_pdf(browser_page, "2_PLACEMENT_ONLY", "Placement Notices (all subjects)", f"Type = PLACEMENT only -- {len(placement)} rows.", placement, show_download=True)
        write_and_pdf(browser_page, "3_INTERNSHIP_ONLY", "Internship Notices (all subjects)", f"Type = INTERNSHIP only -- {len(internship)} rows.", internship, show_download=True)
        browser.close()
    for stale in ("1_ALL_NOTICES", "3_PLACEMENT_NO_PPO", "4_TOP10_INTERNSHIPS"):
        for ext in ("html", "pdf"):
            (DOCS / f"{stale}.{ext}").unlink(missing_ok=True)

    write_json(rows)

    print("\nPlacement + internship documents + notices.json built in ./docs/")

if __name__ == "__main__":
    main()
