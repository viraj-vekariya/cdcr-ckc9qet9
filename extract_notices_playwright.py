#!/usr/bin/env python3
"""Uses a real headless Chromium (Playwright) to load the CDC Notice board and
extract its full jqGrid dataset -- this is the ONLY method confirmed to work
(plain HTTP requests return an empty stub; see notices.py + memory notes).
Takes a cookie string (from login.py), returns the parsed row list.
"""
from playwright.sync_api import sync_playwright

URL = "https://erp.iitkgp.ac.in/TrainingPlacementSSO/Notice.jsp"

def _cookie_dicts(cookie_str):
    out = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        out.append({"name": name.strip(), "value": value.strip(),
                     "domain": "erp.iitkgp.ac.in", "path": "/"})
    return out

def fetch(cookie_str, headless=True, timeout_ms=60000):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context()
        ctx.add_cookies(_cookie_dicts(cookie_str))
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=timeout_ms)
        # wait for jqGrid to finish loading its local dataset
        page.wait_for_function(
            "() => { const t = window.jQuery && window.jQuery('table.ui-jqgrid-btable, table[id^=\"list\"]').first(); "
            "return t && t.length && t.jqGrid('getGridParam','records') > 0; }",
            timeout=timeout_ms,
        )
        rows = page.evaluate("""
        () => {
          const $t = window.jQuery('table.ui-jqgrid-btable, table[id^="list"]').first();
          const data = $t.jqGrid('getGridParam','data');
          // structural strip: turn table rows / <br> / <p> into real line breaks
          // BEFORE removing tags, so e.g. name/roll-number tables land one per line
          // instead of getting glued into one unreadable run-on string.
          const strip = h => {
            if (!h) return '';
            let s = h;
            s = s.replace(/<br\\s*\\/?>/gi, '\\n');
            s = s.replace(/<\\/tr>/gi, '\\n');
            s = s.replace(/<\\/t[dh]>/gi, '  ');
            s = s.replace(/<\\/p>/gi, '\\n\\n');
            s = s.replace(/<[^>]+>/g, ' ');
            s = s.replace(/[ \\t]+/g, ' ');
            s = s.replace(/\\n[ \\t]+/g, '\\n');
            s = s.replace(/[ \\t]+\\n/g, '\\n');
            s = s.replace(/\\n{3,}/g, '\\n\\n');
            return s.trim();
          };
          return data.map(r => ({
            id: r.id, type: strip(r.type).toUpperCase(), subject: strip(r.category),
            company: strip(r.company), notice: strip(r.notice), noticeat: strip(r.noticeat),
            hasDownload: /Download/i.test(r.view1||'')
          }));
        }
        """)
        from cdc_details import enrich_notices, capture_companies
        enrich_notices(ctx, rows)
        try:
            capture_companies(ctx)
        except Exception as exc:
            print(f"Company capture unavailable ({type(exc).__name__}); retaining previous snapshot")
        browser.close()
        return rows

if __name__ == "__main__":
    import sys, json
    cookie = sys.argv[1] if len(sys.argv) > 1 else open("session_cookie.txt").read().strip()
    rows = fetch(cookie, headless=True)
    print(f"fetched {len(rows)} rows")
    json.dump(rows, open("notices_raw_real.json", "w"), indent=1)
