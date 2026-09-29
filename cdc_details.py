"""Read full notice pages and placement opportunity information from ERP.
Never clicks Apply/Cancel, downloads a CV, or saves cookies.
"""
import datetime
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from notice_text import detail_body

BASE = Path(__file__).resolve().parent
ROOT = 'https://erp.iitkgp.ac.in/TrainingPlacementSSO/'
YEAR = os.environ.get('ERP_YEAR', '2026-2027')

def enrich_notices(context, rows):
    previous = {}
    try:
        data = json.loads((BASE / 'docs/notices.json').read_text())
        previous = {str(r['id']): r for group in data['categories'].values() for r in group}
    except (OSError, ValueError, KeyError):
        pass
    pending = []
    for row in rows:
        row['id'] = str(row['id'])
        row['source_hash'] = hashlib.sha256(row['notice'].encode()).hexdigest()
        old = previous.get(row['id'], {})
        if old.get('text_source') == 'full_notice' and old.get('source_hash') == row['source_hash']:
            row['notice'] = old['notice']
            row['text_source'] = 'full_notice'
        else:
            row['text_source'] = 'grid'
            pending.append(row)
    # Placement first, then newest IDs. A bounded backfill avoids long ERP sessions.
    pending.sort(key=lambda r: (r['type'] == 'PLACEMENT', int(r['id']) if r['id'].isdigit() else 0), reverse=True)
    page = context.new_page()
    started = time.monotonic()
    done = 0
    try:
        for row in pending[:40]:
            if time.monotonic() - started > 80:
                break
            try:
                page.goto(ROOT + 'ShowContent.jsp?' + urlencode({'year': YEAR, 'id': row['id']}), timeout=12000)
                area = page.locator('#printableArea')
                text = area.inner_text(timeout=5000)
                if not re.search(r'^Type\s*:', text.strip()):
                    continue
                body = detail_body(text)
                if body and body != text.strip():
                    row['notice'] = body
                    row['text_source'] = 'full_notice'
                    done += 1
            except Exception:
                # Preserve grid fallback, and retry missing details next run.
                continue
    finally:
        page.close()
    print(f'Full notice text: {done} retrieved, {sum(r["text_source"] == "full_notice" for r in rows)} total verified')

def _find_frame(page, url_part, timeout_s=6):
    """Playwright's `page.frames` can lag a moment behind the DOM: the
    iframe's `src` attribute (what `.wait_for()` on a CSS locator checks)
    can be set before Playwright's own frame-tracking has registered a
    matching Frame object with that `.url` -- a single `next(...)` lookup
    right after the locator wait can miss a frame that's genuinely about to
    be there. Confirmed live 29 Sep 2026 in a real (non-headless) browser:
    every selector this module uses is correct and the dialog/iframe/content
    all load fine -- the cloud runner's "job/company detail did not load"
    failures were exactly this race, not a broken selector. Poll instead of
    checking once."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        for f in page.frames:
            if url_part in f.url:
                return f
        time.sleep(0.2)
    return None


def capture_companies(context):
    from attachments import MENU, ENTER
    page = context.new_page()
    results = []
    try:
        page.goto(MENU, timeout=30000)
        page.wait_for_timeout(2000)
        page.evaluate(ENTER)
        # This is the same menu sequence used successfully for attachment downloads.
        page.wait_for_timeout(6000)
        candidates = [f for f in page.frames if 'TPStudent.jsp' in f.url]
        if not candidates:
            raise RuntimeError('CDC application frame unavailable')
        app = candidates[0]
        app.wait_for_function(
            'window.jQuery && jQuery("#grid37").length && '
            'jQuery("#grid37").jqGrid("getGridParam","records") > 0',
            timeout=20000,
        )
        rows = app.locator('#grid37 tr.jqgrow')
        for index in range(min(rows.count(), 30)):
            row = rows.nth(index)
            def cell(field):
                return row.locator(f'[aria-describedby="grid37_{field}"]').inner_text().strip()
            company = cell('companyname')
            role = cell('designation')
            item = dict(company=company, role=role, ctc=cell('ctc'), currency=cell('Currency'),
                        resume_start=cell('resumedeadline_st'), resume_end=cell('resumedeadline'),
                        interview=cell('interview_date_confirmed'), is_applied=None,
                        details='', company_details='')
            item['id'] = hashlib.sha256((company + '\n' + role).encode()).hexdigest()[:16]
            results.append(item)

            # Detail dialogs enrich the card but never prevent the grid data
            # from reaching the site.
            try:
                row.locator('[aria-describedby="grid37_designation"] a').click()
                app.locator('iframe[src*="TPJNFView.jsp"]').wait_for(timeout=12000)
                detail = _find_frame(page, 'TPJNFView.jsp')
                if detail is None:
                    raise RuntimeError('job detail iframe never registered with Playwright')
                detail.locator('#ftpjnfvw').wait_for(timeout=12000)
                text = detail.locator('#ftpjnfvw').inner_text()
                item['is_applied'] = 'Cancel apply' in text
                start = text.find('Company :')
                if start < 0:
                    start = text.find('Company:')
                if start >= 0:
                    item['details'] = text[start:]
            except Exception as exc:
                print(f'Job detail unavailable for {company}: {type(exc).__name__}: {str(exc)[:150]}')
            finally:
                try:
                    app.locator('.ui-dialog:visible .ui-dialog-titlebar-close').last.click(timeout=3000)
                except Exception:
                    pass

            try:
                row.locator('[aria-describedby="grid37_companyname"] a').click()
                app.locator('iframe[src*="TPComView.jsp"]').wait_for(timeout=12000)
                company_frame = _find_frame(page, 'TPComView.jsp')
                if company_frame is None:
                    raise RuntimeError('company detail iframe never registered with Playwright')
                company_frame.get_by_text('Company Details :', exact=True).wait_for(timeout=10000)
                item['company_details'] = company_frame.locator('body').inner_text().replace('Print This Page', '').strip()
            except Exception as exc:
                print(f'Company detail unavailable for {company}: {type(exc).__name__}: {str(exc)[:150]}')
            finally:
                try:
                    app.locator('.ui-dialog:visible .ui-dialog-titlebar-close').last.click(timeout=3000)
                except Exception:
                    pass
        output = {'last_updated': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'companies': results,
                  'scope': 'Placement opportunities visible to the configured ERP account.'}
        (BASE / 'docs/companies.json').write_text(json.dumps(output, indent=2))
        print(f'Placement company details captured: {len(results)}')
    finally:
        page.close()
