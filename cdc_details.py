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

DEPT_HEADING = re.compile(r'^[A-Z][A-Z0-9 &,./()\-]+$')


def _parse_job_detail(text):
    """Splits the raw job-detail dialog text into the pieces a website card
    actually wants, instead of one undifferentiated blob. Verified live 29
    Sep 2026 (Trexquant) against the real dialog structure:
      Cancel apply / View applied Resume
      Company : <name>
      Job Profile
      Form Type<TAB>Designation<TAB>Cost to Company per year<TAB>Bond / Service Contract<TAB>Additional Criteria<TAB>CGPA Cut-off
      PLACEMENT<TAB>Quantitative Researcher<TAB>9160000 INR<TAB><TAB><TAB>8.0
      Job Description
      Description <the actual paragraph, glued to the label with one space>
      Allowed Departments and degrees
      <DEPARTMENT NAME>
      <DEGREE> --- <programme>
      ... (this list can run to hundreds of lines -- e.g. 285 for a role
      open department-wide -- so it's summarised, never dropped: the full
      text is kept in 'eligibility_full' for anyone who wants to check).
    Every field defaults to '' / [] and the caller keeps the raw `details`
    text as a fallback, so a dialog that doesn't match this shape (a
    different company's ERP template, a future ERP change) degrades to
    exactly the old plain-dump behaviour instead of showing something wrong."""
    out = {'form_type': '', 'cgpa_cutoff': '', 'description': '', 'eligibility_summary': '', 'eligibility_full': ''}
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if line.strip() == 'Form Type' and i + 1 < len(lines):
            cells = lines[i + 1].split('\t')
            if len(cells) >= 6:
                out['form_type'] = cells[0].strip()
                out['cgpa_cutoff'] = cells[5].strip()
            break
    i_desc = text.find('Job Description')
    i_dept = text.find('Allowed Departments and degrees')
    if i_desc >= 0:
        desc = text[i_desc + len('Job Description'):(i_dept if i_dept > i_desc else len(text))].strip()
        out['description'] = re.sub(r'^Description\s+', '', desc).strip()
    if i_dept >= 0:
        dept_text = text[i_dept + len('Allowed Departments and degrees'):].strip()
        out['eligibility_full'] = dept_text
        entries = [l.strip() for l in dept_text.split('\n') if l.strip()]
        dept_count = sum(1 for l in entries if DEPT_HEADING.match(l) and '---' not in l)
        programme_count = len(entries) - dept_count
        if dept_count and programme_count <= 8:
            out['eligibility_summary'] = '; '.join(l for l in entries if not (DEPT_HEADING.match(l) and '---' not in l))
        elif dept_count:
            out['eligibility_summary'] = f'Open to {dept_count} department(s), {programme_count} degree programme(s)'
    return out

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


def _frame_dump(page):
    """Diagnostic snapshot for when _find_frame still can't locate a frame
    after polling -- lists every frame Playwright currently knows about, so
    a persistent (not just momentary) mismatch is actually debuggable from
    the run log instead of guessed at again."""
    try:
        return [f.url for f in page.frames]
    except Exception as e:
        return [f'<error dumping frames: {e}>']


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
                        form_type='', cgpa_cutoff='', description='',
                        eligibility_summary='', eligibility_full='',
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
                    raise RuntimeError(f'job detail iframe never registered with Playwright -- frames now: {_frame_dump(page)}')
                detail.locator('#ftpjnfvw').wait_for(timeout=12000)
                text = detail.locator('#ftpjnfvw').inner_text()
                item['is_applied'] = 'Cancel apply' in text
                start = text.find('Company :')
                if start < 0:
                    start = text.find('Company:')
                if start >= 0:
                    item['details'] = text[start:]
                    item.update(_parse_job_detail(item['details']))
            except Exception as exc:
                print(f'Job detail unavailable for {company}: {type(exc).__name__}: {str(exc)[:600]}')
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
                    raise RuntimeError(f'company detail iframe never registered with Playwright -- frames now: {_frame_dump(page)}')
                company_frame.get_by_text('Company Details :', exact=True).wait_for(timeout=10000)
                item['company_details'] = company_frame.locator('body').inner_text().replace('Print This Page', '').strip()
            except Exception as exc:
                print(f'Company detail unavailable for {company}: {type(exc).__name__}: {str(exc)[:600]}')
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
