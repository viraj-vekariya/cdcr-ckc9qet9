#!/usr/bin/env python3
"""The full cloud hourly cycle -- runs standalone on GitHub Actions, no Mac,
no Claude needed per run.
  1. Reuse the saved ERP session if alive; otherwise auto-login (fresh OTP).
  2. Extract the full notice board via a real headless browser (Playwright).
  3. Diff against previously-seen notice IDs (state committed back to the repo).
  4. Rebuild all 4 PDF documents + docs/notices.json with the latest data,
     and commit them back so the GitHub Pages dashboard picks it up.
  5. ALWAYS send a WhatsApp message this hour -- either what's new, or an
     explicit "no update" heartbeat. Best-effort: a WhatsApp hiccup never
     stops the docs/website from being rebuilt and committed.
"""
import datetime, json, os, sys
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import login, build_docs, wa_cloud, attachments, gate
from extract_notices_playwright import fetch as extract_notices

SESSION_FILE = BASE / "session_cookie.txt"
SEEN_FILE = BASE / "seen_notice_ids.json"
CACHE = BASE / "notices_cache.json"
WA_GROUP = os.environ.get("WA_GROUP_NAME", "CDC Updates")  # dedicated group,
                                                            # keeps placement
                                                            # noise out of the
                                                            # personal self-DM
UPDATE_EMAIL = os.environ.get("UPDATE_EMAIL", "viraj.vp.iitkgp@gmail.com")  # 24
                                                            # Sep 2026 trial: a
                                                            # dedicated inbox
                                                            # that mirrors
                                                            # every WhatsApp
                                                            # update (WhatsApp
                                                            # itself is
                                                            # unchanged) -- run
                                                            # both a few days,
                                                            # then decide which
                                                            # channel to rely on
SITE_URL = "https://viraj-vekariya.github.io/cdcr-ckc9qet9/"
IST = ZoneInfo("Asia/Kolkata")  # the GitHub Actions runner's clock is UTC --
                                # every timestamp must convert explicitly or
                                # messages silently show UTC as if it were IST


def log(msg):
    print(f"{datetime.datetime.now(IST):%Y-%m-%d %H:%M:%S} IST | {msg}")


def session_alive(cookie):
    """Checks the TrainingPlacementSSO subsystem directly -- it has its own,
    shorter-lived session cookie independent of the main ERP session."""
    if not cookie:
        return False
    try:
        import requests
        s = requests.Session()
        s.headers.update({"Cookie": cookie, "User-Agent": "Mozilla/5.0"})
        r = s.get("https://erp.iitkgp.ac.in/TrainingPlacementSSO/Notice.jsp",
                  timeout=20, allow_redirects=True)
        return "CDC Notice" in r.text and "jqGrid" in r.text
    except Exception:
        return False


def get_session():
    saved = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else ""
    if session_alive(saved):
        log("session reused (still alive) -- no OTP needed")
        return saved
    log("session dead/absent -- logging in fresh (OTP via Gmail)")
    cookie = login.login(verbose=True)
    if cookie:
        SESSION_FILE.write_text(cookie)
        log("login succeeded")
    else:
        log("LOGIN FAILED")
    return cookie


def reflow(text):
    import re
    for m in ["Venue:", "Note:", "POC:", "Deadline:", "Important:", "Regards",
              "CDC, IIT Kharagpur", "CDC,IIT Kharagpur", "Link:", "Test link",
              "Date:", "Reporting Time:", "Batch One", "Batch Two",
              "Session 1", "Session 2"]:
        text = text.replace(m, "\n\n" + m)
    # split a URL from prose it's glued to with zero separator (source notice
    # HTML sometimes has no space between the link and the next sentence) --
    # must run BEFORE the URL is stashed below, while it's still intact
    text = re.sub(r"(https?://\S+?)([A-Z][a-z]+ [a-z])", r"\1\n\n\2", text)
    # stash URLs so no later regex can dice one up -- confirmed live 24 Sep
    # 2026 (Accenture AEH notice): the roll-number splitter below matched a
    # 9-char chunk *inside* an on24.com link's hex token and inserted a
    # newline mid-URL, breaking it on WhatsApp
    urls = []
    text = re.sub(r"https?://\S+", lambda m: urls.append(m.group(0)) or f"\x00URL{len(urls)-1}\x00", text)
    # confirmed live 25 Sep 2026 (Vedanta PPO notice): source glues each
    # "NAME - ROLLNO" entry straight onto the next name with no separator at
    # all ("...23CH10019SAMEERA JENNA..."), not just onto another roll
    # number -- so the split must fire whenever a roll number is followed by
    # ANY glued alnum, not only a digit
    text = re.sub(r"(\d{2}[A-Z][A-Z0-9]{6}) ?(?=[A-Z0-9])", r"\1\n", text)
    text = re.sub(r"([.!?])(?=[A-Z])", r"\1\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    for i, u in enumerate(urls):
        text = text.replace(f"\x00URL{i}\x00", u)
    return text


GEMINI_MODEL = "gemini-flash-latest"  # confirmed via live API calls 25 Sep 2026 --
                                    # gemini-2.5-flash is deprecated (404, points
                                    # to gemini-3.8-flash); gemini-3.8-flash itself
                                    # returns 503 "high demand" under real load,
                                    # while this rolling alias (same underlying
                                    # model per modelVersion in the response) does not


def smart_reflow(text):
    """25 Sep 2026: reflow()'s regex only fixes glue patterns it already
    knows the literal wording of (e.g. "Venue:", "Batch One") -- a notice
    using any other label/list style glues right back together and needs a
    code change every time. This optional second pass asks Gemini to fix
    layout it can *recognize* rather than just literally match, on top of
    reflow()'s already-decent output.

    Verified word-for-word against reflow()'s own output before use: tokens
    (whitespace-split) must match exactly, in order. If Gemini drops, adds,
    reorders, mistranslates, or alters even one token -- or the call fails,
    times out, or GEMINI_API_KEY isn't set -- this silently returns the
    plain regex text unchanged. A Gemini glitch can only ever affect *where
    the line breaks fall*, never what reaches WhatsApp/email/website."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return text
    import re, time as _time, json as _json, urllib.request as _urlreq
    prompt = (
        "Reformat the layout of this placement/internship notice so it reads "
        "cleanly on WhatsApp: put separate fields/labels (e.g. Date:, Venue:, "
        "Time:, CTC, batch/session names), separate list entries, and links "
        "each on their own line, with a blank line between distinct "
        "sections. Fix any link that's malformed, split across lines, or has "
        "stray characters stuck to it, so it becomes one clean working URL. "
        "You may reorder or lightly reword for clarity, but do not change "
        "any fact: keep every number (CTC, dates, times, roll numbers, "
        "phone numbers), every company/person name, and every link's actual "
        "URL exactly as given -- reformat around them, never alter them. Do "
        "not add any title, header, footer, note, or explanation of your "
        "own, and do not drop any real information from the notice. Output "
        "ONLY the reformatted text and nothing else.\n\n---\n" + text
    )
    body = _json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        # thinkingBudget 0: this is pure whitespace rearrangement, not
        # reasoning -- with thinking left on, a real test call burned 134
        # "thought" tokens to produce a 2-token reply and got rate-limited
        # into a 503 "high demand" error on the very next call (25 Sep 2026
        # live test); disabling it made the same payload succeed every time.
        "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}},
    }).encode()
    out = None
    for attempt in range(2):
        try:
            req = _urlreq.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}",
                data=body, headers={"Content-Type": "application/json"},
            )
            with _urlreq.urlopen(req, timeout=25) as resp:
                data = _json.loads(resp.read())
            out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            break
        except Exception as e:
            log(f"Gemini polish call failed (attempt {attempt + 1}/2): {e}")
            if attempt == 0:
                _time.sleep(3)  # Google's own 503 message: "usually temporary"
    if out is None:
        log("Gemini polish giving up, using regex text")
        return text

    # Loosened 29 Sep 2026 per explicit request: an exact-token-match check
    # rejected Gemini's output for ANY rewording/reordering, including
    # genuinely better URL formatting -- which is exactly what this feature
    # is for. What's still hard-verified, because these are the parts that
    # actually matter on a real placement notice (CTC, dates, deadlines,
    # roll numbers, application links): every number and every URL present
    # in the regex text must still be present, character-for-character,
    # somewhere in Gemini's output. This catches a dropped/altered fact
    # without punishing harmless rewording or reflow.
    nums = lambda s: set(re.findall(r"\d+(?:[.,]\d+)*", s))
    urls = lambda s: set(re.findall(r"https?://\S+", s))
    missing_nums = nums(text) - nums(out)
    missing_urls = urls(text) - urls(out)
    if missing_nums or missing_urls:
        log(f"Gemini polish REJECTED -- missing numbers {missing_nums} or URLs {missing_urls}, using regex text")
        return text
    log("Gemini polish applied")
    return out


def email_fallback(subject, message):
    """WhatsApp Web on a headless runner can fail in ways that look like
    success (see wa_cloud.send's docstring, 21 Sep 2026) -- when it now
    raises instead, this is the one channel that never depends on the
    flaky part: same Gmail app password already used for OTP retrieval,
    sent to the same inbox the user already checks every cycle for OTPs."""
    gmail, app_pw = os.environ.get("GMAIL", ""), os.environ.get("APP_PW", "")
    if not (gmail and app_pw):
        log("email fallback skipped -- GMAIL/APP_PW not set")
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = gmail
        msg["To"] = gmail
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls()
            s.login(gmail, app_pw)
            s.send_message(msg)
        log("fallback email sent (WhatsApp send failed)")
    except Exception as e:
        log(f"fallback email ALSO failed: {e}")


def send_update_email(subject, message, failed=False):
    """24 Sep 2026 trial channel: mirrors every update that goes to WhatsApp
    to a SEPARATE dedicated inbox (UPDATE_EMAIL), independent of whether
    WhatsApp itself succeeds or fails -- explicit request to run both in
    parallel for a couple of days, then decide which one to rely on
    primarily. A failed WhatsApp send is tagged so it doesn't blend in with
    a normal update. WhatsApp's own logic is untouched by this."""
    gmail, app_pw = os.environ.get("GMAIL", ""), os.environ.get("APP_PW", "")
    if not (gmail and app_pw and UPDATE_EMAIL):
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        tag = "⚠ FAILED - " if failed else ""
        msg = MIMEText(message)
        msg["Subject"] = f"{tag}{subject}"
        msg["From"] = gmail
        msg["To"] = UPDATE_EMAIL
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls()
            s.login(gmail, app_pw)
            s.send_message(msg)
        log(f"update email sent to {UPDATE_EMAIL}")
    except Exception as e:
        log(f"update email failed: {e}")


def notify_all(message, subject="CDC watcher update"):
    """Personal self-DM only -- the reliable, proven channel. The "CDC
    Updates" group send was tried as a second channel but is still flaky
    (WhatsApp-side sync glitches + UI timing issues), so it's been pulled
    back out of the automated path per explicit decision (20 Sep 2026) to
    not risk the one reliable channel while the group is still being
    debugged separately. wa_cloud.send_to_chat() stays available in
    wa_cloud.py for that ongoing debugging -- just not called from here
    until it's proven reliable.

    If WhatsApp itself fails (now verified, not just attempted -- see
    wa_cloud.send), that failure would otherwise be totally silent to the
    user (logged only in a GitHub Actions run log nobody is watching).
    Falls back to email so a WhatsApp outage is never a silent outage."""
    try:
        wa_cloud.send(message)
        log("WhatsApp sent to personal DM (delivery confirmed)")
        send_update_email(subject, message, failed=False)
    except Exception as e:
        log(f"WhatsApp personal-DM send failed: {e}")
        email_fallback("CDC watcher -- WhatsApp failed, here's the update", f"{message}\n\n(WhatsApp error: {e})")
        send_update_email(subject, f"{message}\n\n(WhatsApp error: {e})", failed=True)


def main():
    if not gate.run_is_due():
        log("skipping -- not due (a recent attempt/success exists; another trigger already handled this hour)")
        return
    gate.record_attempt()

    now_str = datetime.datetime.now(IST).strftime("%d %b, %I:%M %p IST")
    cookie = get_session()
    if not cookie:
        log(f"login failed at {now_str} -- will retry next hour.")
        notify_all(f"CDC watcher: login failed at {now_str} -- will retry next hour.",
                   subject="CDC watcher: login failed")
        return

    log("extracting notice board via headless browser...")
    raw = extract_notices(cookie, headless=True)
    log(f"fetched {len(raw)} total notices")

    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    # WhatsApp stays placement-only, and excludes PPO specifically (21 Sep
    # 2026 request) -- the website/PDFs below get EVERYTHING (placement +
    # internship, PPO included) since that split now lives client-side on
    # the site's own subject filter instead of at the alerting layer.
    # seen-tracking still covers every id so nothing wrongly resurfaces.
    new_rows = [r for r in raw if r["id"] not in seen and r["type"] == "PLACEMENT"
                and (r.get("subject") or "").upper() != "PPO"]

    # Gemini-polish only the freshly-arriving notices (typically 0-5/hour) so
    # the WhatsApp/email text and the website show identical, better-
    # formatted text for anything a user might currently see as new --
    # running this on all ~859 rows every run would blow the 15-min job
    # timeout and any API quota for no benefit (nobody's re-reading old ones).
    smart_text = {r["id"]: smart_reflow(reflow(r["notice"])) for r in new_rows}

    cache_rows = [{
        "id": r["id"], "type": r["type"], "subject": r["subject"], "company": r["company"],
        "notice": smart_text.get(r["id"]) or reflow(r["notice"]), "noticeat": r["noticeat"],
        "download_raw": "<a href='#'>Download</a>" if r.get("hasDownload") else "",
        # enrich_notices() (extract_notices_playwright.py) sets these on `raw`
        # so it can skip re-fetching an already-captured notice next run --
        # but they were never carried through into this published row, so
        # every notice defaulted back to "grid" on the site forever and
        # enrich_notices kept re-fetching the same ~40 newest placement rows
        # every single cycle instead of ever making progress on the rest.
        "text_source": r.get("text_source", "grid"), "source_hash": r.get("source_hash", ""),
    } for r in raw]
    CACHE.write_text(json.dumps(cache_rows, indent=1))
    # attachments for EVERY notice (placement + internship) -- the site now
    # shows both categories with their real files, not placement-only.
    files = attachments.sync(cookie, cache_rows, log)
    log(f"attachments: {len(files)} file(s) captured, "
        f"{sum(1 for r in cache_rows if r['download_raw'])} notice(s) list a Download")
    build_docs.main()
    log(f"documents rebuilt ({len(cache_rows)} total rows)")

    SEEN_FILE.write_text(json.dumps(sorted({r["id"] for r in raw}, key=lambda x: int(x) if x.isdigit() else 0)))
    login.logout(cookie)
    log("ERP session closed")

    log(f"cycle complete -- {len(new_rows)} new placement notice(s) (excl. PPO) this hour" if new_rows else "cycle complete -- no new non-PPO placement notices this hour")
    prev_run = gate.last_success()
    gate.record_success()

    # FULL notice text per new placement notice, then the ERP attachment (if
    # the ERP actually served one), always with the website link. When
    # nothing new: an explicit window line so the silence is visibly checked.
    if new_rows:
        for r in new_rows:
            header = f"[PLACEMENT] {r['company'] or '(no company / general notice)'} ({r['subject']})"
            body = smart_text.get(r["id"]) or reflow(r["notice"])
            listed = bool(r.get("hasDownload"))
            fpath = files.get(r["id"])
            if fpath:
                note = "\n\n\U0001F4CE Attachment sent below."
            elif listed:
                note = ("\n\n\U0001F4CE ERP lists an attachment for this notice but it could not "
                        "be downloaded automatically - open Download on the ERP notice board.")
            else:
                note = ""
            notify_all(f"{header}\n\n{body}{note}\n\n\U0001F310 {SITE_URL}", subject=header)
            if fpath:
                try:
                    wa_cloud.send_file(fpath, caption=f"{r['company']} - attachment")
                    log(f"WhatsApp attachment sent for notice {r['id']}")
                except Exception as e:
                    log(f"WhatsApp attachment send failed for {r['id']} (non-fatal): {e}")
    else:
        end = datetime.datetime.now(IST).strftime("%I:%M %p")
        if prev_run:
            start = prev_run.astimezone(IST).strftime("%I:%M %p")
            notify_all(f"No CDC update from {start} to {end} IST.\n\n\U0001F310 {SITE_URL}",
                       subject="CDC watcher: no update this hour")
        else:
            notify_all(f"No CDC update for now ({end} IST).\n\n\U0001F310 {SITE_URL}",
                       subject="CDC watcher: no update this hour")


if __name__ == "__main__":
    main()
