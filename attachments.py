#!/usr/bin/env python3
"""Downloads the ERP "Download" attachment of PLACEMENT notices into docs/files/.

Why a browser: the grid's Download link calls TPNotice(year, id), which loads
TrainingPlacementSSO/AdmFilePDF.htm?type=NOTICE&year=..&id=.. But that endpoint
only serves real bytes when the session entered the placement module through
the ERP's own menu (IIT_ERP3 menulist -> showMenu(... TPStudent.jsp) ->
Notice). Opening Notice.jsp directly, or GETting AdmFilePDF.htm with a plain
HTTP client, gets 200 / application/pdf / Content-Length: 0 for every id.
So this replays the menu path in headless Chromium and uses the real download.
"""
import json, os
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
FILES = BASE / "docs" / "files"
YEAR = os.environ.get("ERP_YEAR", "2026-2027")
MENU = "https://erp.iitkgp.ac.in/IIT_ERP3/menulist.htm?module_id=26"
ENTER = ("showMenu('26','11','https://erp.iitkgp.ac.in/TrainingPlacementSSO/TPStudent.jsp',"
         "'','Y','CDC','Student','Application of Placement/Internship')")
MAX_PER_RUN = 25


def _cookie_dicts(cookie_str):
    out = []
    for part in cookie_str.split(";"):
        if "=" in part:
            n, v = part.split("=", 1)
            out.append({"name": n.strip(), "value": v.strip(),
                        "domain": "erp.iitkgp.ac.in", "path": "/"})
    return out


def _download_all(cookie, ids, log):
    got = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True, viewport={"width": 1400, "height": 900})
        ctx.add_cookies(_cookie_dicts(cookie))
        page = ctx.new_page()
        page.goto(MENU, timeout=60000)
        page.wait_for_timeout(2000)
        page.evaluate(ENTER)
        page.wait_for_timeout(6000)
        tp = [f for f in page.frames if "TPStudent" in f.url]
        if not tp:
            log("attachments: could not enter the placement module")
            browser.close()
            return got
        tp[0].click('a[href="Notice.jsp"]')
        page.wait_for_timeout(6000)
        nf = [f for f in page.frames if "Notice.jsp" in f.url]
        if not nf:
            log("attachments: Notice frame never opened")
            browser.close()
            return got
        nf = nf[0]
        nf.wait_for_function(
            "()=>{const t=window.jQuery&&window.jQuery('table.ui-jqgrid-btable').first();"
            "return t&&t.length&&t.jqGrid('getGridParam','records')>0;}", timeout=60000)
        for nid in ids:
            try:
                with page.expect_download(timeout=20000) as d:
                    nf.evaluate(f"TPNotice('{YEAR}','{nid}')")
                tmp = FILES / f".{nid}.part"
                d.value.save_as(str(tmp))
                data = tmp.read_bytes()
                tmp.unlink(missing_ok=True)
                if data[:4] == b"%PDF":
                    got[nid] = data
                else:
                    log(f"attachments: notice {nid} returned {len(data)} bytes, not a PDF -- skipped")
            except Exception as e:
                log(f"attachments: notice {nid} download failed: {str(e)[:90]}")
            page.wait_for_timeout(800)
        browser.close()
    return got


def sync(cookie, rows, log=print):
    """rows: notice rows (dicts with id/download_raw/file_id). Makes sure
    docs/files/<row id>.pdf holds the attachment of THAT notice. Returns
    {id: Path} for ids that have a file.

    7 Nov 2026: files used to be keyed only by the grid row id, which ERP
    renumbered after deleting two test notices -- Fujitsu's PDF stayed under
    its old id and showed up on a HiLabs notice. manifest.json now records
    which ERP file id each saved PDF came from; a file whose row now points at
    a different file id is moved or re-downloaded, and a stale one removed."""
    FILES.mkdir(parents=True, exist_ok=True)
    mpath = FILES / "manifest.json"
    try:
        manifest = json.loads(mpath.read_text())
    except Exception:
        manifest = {}
    # PDFs saved before the manifest existed were fetched with TPNotice(row id),
    # so their file id is their own name.
    for f in FILES.glob("*.pdf"):
        if f.stat().st_size > 0:
            manifest.setdefault(f.stem, f.stem)
    holder = {fid: rid for rid, fid in manifest.items() if (FILES / f"{rid}.pdf").exists()}
    want, unknown = {}, set()
    for r in rows:
        if (r.get("download_raw") or "").strip():
            if r.get("file_id"):
                want[r["id"]] = str(r["file_id"])
            else:
                # the Download link didn't expose its file id: old behaviour,
                # trust an existing file and never treat it as stale
                want[r["id"]] = manifest.get(r["id"], r["id"])
                unknown.add(r["id"])

    have, todo, moves = {}, {}, {}
    for rid, fid in want.items():
        if rid in unknown and (FILES / f"{rid}.pdf").exists():
            have[rid] = FILES / f"{rid}.pdf"
        elif manifest.get(rid) == fid and (FILES / f"{rid}.pdf").exists():
            have[rid] = FILES / f"{rid}.pdf"
        elif fid in holder:
            moves[rid] = holder[fid]
        else:
            todo[fid] = rid
    # read every source first so a move can't clobber another move's source
    blobs = {rid: (FILES / f"{src}.pdf").read_bytes() for rid, src in moves.items()}
    for rid, data in blobs.items():
        (FILES / f"{rid}.pdf").write_bytes(data)
        manifest[rid] = want[rid]
        have[rid] = FILES / f"{rid}.pdf"
        log(f"attachments: notice {rid} file moved from {moves[rid]}.pdf (ERP renumbered it)")
    if todo:
        try:
            got = _download_all(cookie, list(todo)[:MAX_PER_RUN], log)
            for fid, data in got.items():
                rid = todo[fid]
                (FILES / f"{rid}.pdf").write_bytes(data)
                manifest[rid] = fid
                have[rid] = FILES / f"{rid}.pdf"
        except Exception as e:
            log(f"attachments: download pass failed (non-fatal): {str(e)[:120]}")
    # 7 Nov 2026 (second fix): only remove a saved PDF when ERP lists NO
    # attachment for that notice any more. A mismatched file is replaced only
    # once a real replacement downloaded -- the first version deleted the only
    # copy of Fujitsu's PDF (ERP's own Download for it serves 0 bytes) and the
    # files of notices that had simply dropped off the board.
    listed = {r["id"]: bool((r.get("download_raw") or "").strip()) for r in rows}
    for rid in list(manifest):
        p = FILES / f"{rid}.pdf"
        if rid in have or not p.exists():
            continue
        if listed.get(rid) is False:
            p.unlink()
            log(f"attachments: removed {rid}.pdf -- that notice now lists no attachment (ERP renumbered it)")
            manifest.pop(rid)
        elif listed.get(rid):
            have[rid] = p
            log(f"attachments: kept {rid}.pdf (from ERP file {manifest[rid]}); ERP file {want.get(rid)} could not be fetched")
    mpath.write_text(json.dumps(manifest, indent=0, sort_keys=True))
    return have
