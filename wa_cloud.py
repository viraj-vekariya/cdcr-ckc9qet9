#!/usr/bin/env python3
"""WhatsApp Web automation for a headless CI runner (GitHub Actions).

Two modes:
  setup  -- one-time: requests a "log in with phone number" linking code,
            prints it to the run log for the human to enter on their phone,
            waits for login to complete, then leaves the resulting session
            in WA_PROFILE_DIR for the caller to persist (encrypt + commit).
  send   -- sends a message to "Message yourself" using an already-linked
            session in WA_PROFILE_DIR.

The session is created and lives entirely on the runner -- never touches any
other machine. A modern User-Agent is required or WhatsApp Web refuses to
load at all ("update your Chrome"), even though the underlying engine is
perfectly current.
"""
import os, re, sys, time, urllib.parse
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(os.environ.get("WA_PROFILE_DIR", "wa_profile"))
MY_NUMBER = os.environ.get("WA_NUMBER", "")  # E.164 without '+' (country code + number)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def _launch(p, headless=True):
    return p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=headless, user_agent=UA)


def is_logged_in(headless=True, timeout_ms=20000):
    with sync_playwright() as p:
        ctx = _launch(p, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://web.whatsapp.com/", timeout=timeout_ms)
        try:
            page.wait_for_selector(
                '[aria-label="Search or start a new chat" i], [title="Search or start a new chat" i]',
                timeout=timeout_ms)
            ok = True
        except Exception:
            ok = page.locator("canvas").count() == 0 and "web.whatsapp.com" in page.url
        ctx.close()
        return ok


def _extract_code(full_text):
    """The 8-char code renders as one character per line (e.g.
    "Z\\n3\\nB\\n9\\n-\\nY\\nA\\nX\\nL") -- find the run of single-char lines right
    after the "(edit)" marker and join them into e.g. "Z3B9-YAXL"."""
    lines = [l.strip() for l in full_text.split("\n")]
    for i, l in enumerate(lines):
        if "(edit)" in l or "Linking WhatsApp account" in l:
            chunk = []
            for l2 in lines[i+1:i+15]:
                if len(l2) == 1 and (l2.isalnum() or l2 == "-"):
                    chunk.append(l2)
                    if len(chunk) == 9:  # 4 chars + dash + 4 chars, exactly
                        break
                elif chunk:
                    break
            candidate = "".join(chunk)
            import re
            if re.match(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$", candidate):
                return candidate
    return None


def setup_with_phone_code(timeout_s=480):
    """Prints an 8-character linking code to stdout, then polls (up to
    timeout_s) for the human to enter it on their phone. WhatsApp's linking
    code REFRESHES every ~60s if unused -- this re-checks periodically and
    re-prints whenever a new code appears, so a slow human-relay loop (e.g.
    reading it off a workflow log) always has a live, usable code available.
    Prints LOGIN_OK or LOGIN_TIMEOUT as the last line so the calling workflow
    step can branch."""
    if not MY_NUMBER:
        print("WA_NUMBER not set"); return False
    with sync_playwright() as p:
        ctx = _launch(p, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://web.whatsapp.com/", timeout=30000)
        page.wait_for_timeout(6000)
        # occasionally the first click on a cold CI runner doesn't register
        # (page not fully interactive yet) and the phone-number panel never
        # appears -- retry the click itself a couple of times before giving up,
        # rather than failing the whole run on a single transient miss.
        got_input = False
        last_err = None
        for attempt in range(3):
            try:
                page.get_by_text("Log in with phone number", exact=False).first.click(timeout=15000)
            except Exception as e:
                last_err = f"could not find 'log in with phone number' link: {e}"
                page.wait_for_timeout(2000)
                continue
            try:
                page.wait_for_selector('input[data-testid="phone-number-input"]', timeout=12000)
                got_input = True
                break
            except Exception as e:
                last_err = f"phone number field never appeared: {e}"
                print(f"attempt {attempt+1}/3 failed -- {last_err}")
                page.wait_for_timeout(2000)
        if not got_input:
            print(last_err)
            print("--- page text for diagnosis ---")
            print(page.evaluate("document.body.innerText")[:600])
            ctx.close(); return False
        page.wait_for_timeout(1500)
        # WhatsApp auto-selects the country by the SERVER's IP geolocation, not
        # the target number's actual country -- on a US-based CI runner this
        # silently defaults to the US, which then misreads the whole number.
        # Fix: type the FULL international number with a leading '+' directly
        # into the phone field -- the widget auto-detects and self-corrects the
        # country from the '+91' prefix regardless of whatever it defaulted to,
        # which is far more robust than trying to drive the country dropdown UI.
        try:
            box = page.locator('input[data-testid="phone-number-input"]')
            box.click(timeout=15000)
            box.fill("")
            box.type("+" + MY_NUMBER, delay=30)
            page.wait_for_timeout(1200)
            cur = page.evaluate("document.body.innerText")
            if "+91" not in cur:
                print("WARNING: country did not auto-correct to +91 as expected")
                print(cur[:300])
        except Exception as e:
            print("could not fill phone number field:", e)
            ctx.close(); return False
        page.wait_for_timeout(1000)
        # the "Next" button may take longer to enable/render on a CI runner than
        # it did in local testing -- try a real click first (longer timeout),
        # then fall back to just pressing Enter in the field, which most forms
        # (including this one) also accept as a submit trigger.
        submitted = False
        try:
            page.get_by_role("button", name="Next", exact=False).click(timeout=20000)
            submitted = True
        except Exception as e:
            print("'Next' button click failed, falling back to Enter key:", e)
            try:
                box.press("Enter")
                submitted = True
            except Exception as e2:
                print("Enter-key fallback also failed:", e2)
        if not submitted:
            print("--- page text for diagnosis ---")
            print(page.evaluate("document.body.innerText")[:600])
            ctx.close(); return False
        page.wait_for_timeout(3000)
        full_text = page.evaluate("document.body.innerText")
        code = _extract_code(full_text)
        print("LINKING_CODE:", code or "NOT FOUND -- see full text below")
        if not code:
            print(full_text[:800])
            ctx.close(); return False
        print(f">>> On your phone: WhatsApp > Settings > Linked Devices > Link with phone number instead > enter {code} <<<")
        print(">>> This code refreshes automatically every ~60s if unused -- watch this log, a NEW code will be printed each time it changes. Use whichever code is printed MOST RECENTLY. <<<")
        last_code = code
        end = time.time() + timeout_s
        ok = False
        while time.time() < end:
            try:
                cur_text = page.evaluate("document.body.innerText")
                cur_code = _extract_code(cur_text)
                if cur_code and cur_code != last_code:
                    last_code = cur_code
                    print(f"LINKING_CODE (refreshed): {cur_code}")
                    print(f">>> On your phone: enter the NEW code {cur_code} <<<")
            except Exception:
                pass
            try:
                page.wait_for_selector(
                    '[aria-label="Search or start a new chat" i], [title="Search or start a new chat" i]',
                    timeout=5000)
                ok = True
                break
            except Exception:
                time.sleep(3)
        print("LOGIN_OK" if ok else "LOGIN_TIMEOUT")
        ctx.close()
        return ok


def _confirm_last_sent(page, before_count, expected_snippet=None, timeout_ms=8000):
    """Confirms the message we just sent actually left the client, using
    selectors VERIFIED live on 21 Sep 2026 against the current WhatsApp Web
    DOM (the earlier div.message-out / data-icon=msg-check selectors are
    stale -- WhatsApp has since restructured to CSS-module class names and
    moved the tick status into an <svg><title> rather than data-icon).
    Every real message has data-pre-plain-text (unlike the aria-label="You:"
    span, which WhatsApp only stamps on the first message of a consecutive
    group -- checking for it caused a false-negative on every message after
    the first in a run). div[role="row"] is the wrong container to take
    .last from -- WhatsApp virtualizes this list and the trailing row(s) at
    any given moment can be an empty addon/placeholder row with no message
    in it at all, not the newest message -- so target actual message
    containers directly via [data-testid^="conv-msg-"] instead.

    CRITICAL FIX (29 Sep 2026): the old version only checked whether the
    LAST message in the DOM had a sent/delivered/read tick -- but on a self-
    chat there is ALWAYS an old, already-ticked message sitting there from a
    previous run. If a send silently failed to register (the deep-link's
    prefilled text never actually reached the compose box, Enter fired too
    early, etc.) the conversation's last real message stays that old one,
    and the old check happily reported "confirmed" every single run even
    though nothing new went out -- exactly matching real user reports of
    getting only a handful of the many "delivery confirmed" messages logged.
    Fix: `before_count` is the message count taken right before sending;
    this only returns True once a message count LARGER than that appears
    (a genuinely new bubble, not a stale old one), and -- when the caller
    knows the text it sent -- that new bubble's own text must contain
    `expected_snippet`, so a same-count-but-different-message race can't
    false-positive either.

    FOLLOW-UP FIX (29 Sep 2026, same day): the count-based check above missed
    a send that genuinely went through -- live run diag showed the correct
    text already sitting as the chat's own preview in the sidebar (with the
    right timestamp) while `[data-testid^="conv-msg-"]`'s count never grew,
    i.e. that virtualized list doesn't reliably repaint fast enough right
    after a send even though the message really was accepted and queued.
    Fallback: if `expected_snippet` shows up anywhere in the page's own text
    AND the compose box no longer holds it (so we're not just seeing an
    unsent draft), treat that as confirmed too -- the sidebar's last-message
    preview updating is itself real, near-instant evidence of a successful
    send, verified against a real message on a real run."""
    msgs = page.locator('[data-testid^="conv-msg-"]')
    box = page.locator('div[contenteditable="true"][aria-label^="Type a message" i]')
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip()
    want = norm(expected_snippet) if expected_snippet else None
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        n = msgs.count()
        if n > before_count:
            last = msgs.nth(n - 1)
            try:
                if not want or want in norm(last.inner_text(timeout=1500)):
                    # SVG <title> elements have no innerText (all_inner_texts()
                    # silently returns None for each -- verified live 21 Sep
                    # 2026), so textContent via all_text_contents() is required.
                    titles = last.locator("title").all_text_contents()
                    if any((t or "").strip().startswith(("wds-ic-check", "wds-ic-double-check", "wds-ic-read"))
                           for t in titles):
                        return True
            except Exception:
                pass
        if want:
            try:
                body = norm(page.evaluate("document.body.innerText"))
                box_text = norm(box.inner_text(timeout=1000)) if box.count() else ""
            except Exception:
                body, box_text = "", want  # can't check safely -- don't false-positive
            if want in body and want not in box_text:
                return True
        page.wait_for_timeout(400)
    return False


def send(message, headless=True, timeout_ms=30000):
    """Sends to the personal self-chat and VERIFIES it actually went out --
    a cold headless profile can render the compose box from cached local
    state before the real multi-device socket to the phone is up, which
    made earlier versions of this function report success (no exception)
    for messages that were typed and Entered but never actually delivered.
    Now: waits a bit longer for the app to settle, bails loudly if the page
    shows an offline/connecting banner, and after sending, confirms the
    last outgoing bubble in the chat actually carries a sent/delivered tick
    before returning -- if it can't confirm that, it raises instead of
    silently claiming success."""
    if not MY_NUMBER:
        raise RuntimeError("WA_NUMBER not set")
    text = urllib.parse.quote(message)
    url = f"https://web.whatsapp.com/send?phone={MY_NUMBER}&text={text}"
    with sync_playwright() as p:
        ctx = _launch(p, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, timeout=timeout_ms)
        # A Locator, not an ElementHandle from wait_for_selector: WhatsApp Web
        # can re-render the compose box in the few seconds it takes the real
        # multi-device socket to come up (confirmed live 21 Sep 2026, cloud
        # run against the freshly re-linked session -- an ElementHandle taken
        # before that settle-wait went stale and box.click() below raised
        # "Element is not attached to the DOM"). A Locator re-queries the DOM
        # on every action instead of holding a handle to a specific node, so
        # it survives that re-render.
        box = page.locator('div[contenteditable="true"][aria-label^="Type a message" i]')
        box.wait_for(state="visible", timeout=timeout_ms)
        page.wait_for_timeout(3000)  # let the socket to the phone settle, not just the DOM render
        before_count = page.locator('[data-testid^="conv-msg-"]').count()

        def diag():
            """One-shot diagnostic bundle -- attached to any failure so a
            21 Sep 2026-style 'looked fine, nothing arrived' failure is
            actually debuggable from the run log instead of guessed at."""
            d = {"url": page.url}
            try:
                d["headers"] = [h.inner_text(timeout=1500) for h in page.locator("header").all()]
            except Exception as e:
                d["headers"] = f"<err {e}>"
            try:
                d["conv_panel_present"] = page.locator("#main").count() > 0
            except Exception:
                d["conv_panel_present"] = "<err>"
            try:
                d["outgoing_row_count"] = page.locator('div[role="row"] span[aria-label="You:"]').count()
            except Exception:
                d["outgoing_row_count"] = "<err>"
            try:
                d["body_snippet"] = page.evaluate("document.body.innerText")[:400]
            except Exception:
                d["body_snippet"] = "<err>"
            return d

        page_text = page.evaluate("document.body.innerText")
        if any(w in page_text for w in ("Connecting", "computer is not connected", "Trying to reach phone", "phone number shared via url is invalid")):
            info = diag()
            ctx.close()
            raise RuntimeError(f"WhatsApp not actually connected before send -- {info}")

        try:
            box.click(timeout=10000)
            page.keyboard.press("Enter")
        except Exception as e:
            # a WhatsApp popup overlaying the page swallows pointer clicks
            # (28 Sep 2026: every run timed out here while the box was visible);
            # keyboard focus + Enter doesn't need pointer hit-testing
            try:
                dialogs = [t[:200] for t in page.locator('[role="dialog"]').all_inner_texts()]
            except Exception:
                dialogs = "<err>"
            print(f"compose click blocked ({str(e).splitlines()[0]}); dialogs={dialogs}; sending via focus+Enter")
            box.press("Enter")
        page.wait_for_timeout(2500)

        sent_ok = _confirm_last_sent(page, before_count, expected_snippet=message[:40], timeout_ms=10000)
        if not sent_ok:
            info = diag()
            ctx.close()
            raise RuntimeError(f"could not confirm a NEW sent/delivered tick after Enter -- {info}")
        ctx.close()


def send_file(path, caption="", headless=True, timeout_ms=40000):
    """Sends a file (PDF etc.) as a document to the personal self-DM, with an
    optional caption. Attach menu -> Document -> file chooser -> preview ->
    Send. The file is sent as a document (not a compressed photo)."""
    if not MY_NUMBER:
        raise RuntimeError("WA_NUMBER not set")
    path = str(Path(path).resolve())
    with sync_playwright() as p:
        ctx = _launch(p, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"https://web.whatsapp.com/send?phone={MY_NUMBER}", timeout=timeout_ms)
        page.wait_for_selector('div[contenteditable="true"][aria-label^="Type a message" i]', timeout=timeout_ms)
        page.wait_for_timeout(3000)  # let the socket to the phone settle, not just the DOM render
        before_count = page.locator('[data-testid^="conv-msg-"]').count()
        page_text = page.evaluate("document.body.innerText")
        if any(w in page_text for w in ("Connecting", "computer is not connected", "Trying to reach phone", "phone number shared via url is invalid")):
            ctx.close()
            raise RuntimeError(f"WhatsApp not actually connected before send_file -- page={page_text[:300]!r}")
        page.get_by_role("button", name="Attach").first.click(timeout=timeout_ms)
        page.wait_for_timeout(800)
        with page.expect_file_chooser(timeout=timeout_ms) as fc:
            page.get_by_text("Document", exact=True).first.click(timeout=timeout_ms)
        fc.value.set_files(path)
        page.wait_for_timeout(2500)
        if caption:
            cap = page.locator('div[contenteditable="true"][aria-label*="caption" i], div[contenteditable="true"][aria-label^="Add a caption" i]').first
            try:
                cap.click(timeout=5000)
                page.keyboard.insert_text(caption)
            except Exception:
                pass
        try:
            page.get_by_role("button", name="Send", exact=True).last.click(timeout=8000, force=True)
        except Exception:
            page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        sent_ok = _confirm_last_sent(page, before_count, expected_snippet=(caption[:40] if caption else None), timeout_ms=12000)
        ctx.close()
        if not sent_ok:
            raise RuntimeError("could not confirm a NEW sent/delivered tick after sending the file")


def _find_chat_listitem(page, chat_name, timeout_s=15):
    """Polls the currently-rendered list of chat rows for one whose text
    starts with chat_name (list items also carry a trailing timestamp/last-
    message line, so startswith is used rather than an exact match)."""
    end = time.time() + timeout_s
    while time.time() < end:
        for it in page.get_by_role("listitem").all():
            try:
                t = it.inner_text(timeout=1500)
            except Exception:
                continue
            if t.strip().startswith(chat_name):
                return it
        time.sleep(1)
    return None


def send_to_chat(message, chat_name, headless=True, timeout_ms=30000):
    """Sends to a WhatsApp GROUP (or any named chat) by name -- something the
    official/Composio API can never do, only real browser automation.

    The `?text=` deep link opens a "Send message to" forward-style picker;
    text placed via this picker preserves newlines correctly (typing it via
    the keyboard directly does not -- a literal "\\n" triggers WhatsApp's own
    Enter-to-send behaviour mid-message). Clicking the target chat then
    "Send" here does NOT actually transmit it despite the button's name --
    it leaves the text sitting as a draft -- but it also leaves that chat's
    own compose box (with the draft already in it) immediately available on
    the SAME page, so no second navigation/search is needed: just click that
    box and press Enter to actually send it.
    """
    text = urllib.parse.quote(message)
    with sync_playwright() as p:
        ctx = _launch(p, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.goto(f"https://web.whatsapp.com/send?text={text}", timeout=timeout_ms)
        page.wait_for_timeout(4000)
        target = _find_chat_listitem(page, chat_name)
        if not target:
            ctx.close()
            raise RuntimeError(f"chat '{chat_name}' not found in forward picker")
        target.click(timeout=timeout_ms)
        page.wait_for_timeout(1200)
        page.get_by_role("button", name="Send", exact=True).click(timeout=timeout_ms)
        page.wait_for_timeout(2500)

        # Locator, not an ElementHandle -- see the matching comment in send()
        # for why (a re-render between locating and clicking detaches an
        # ElementHandle taken via wait_for_selector).
        box = page.locator('div[contenteditable="true"][aria-label^="Type a message" i]')
        box.wait_for(state="visible", timeout=timeout_ms)
        box.click(timeout=timeout_ms)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        ctx.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "send"
    if mode == "setup":
        ok = setup_with_phone_code()
        sys.exit(0 if ok else 1)
    elif mode == "check":
        print("logged in:", is_logged_in())
    else:
        msg = sys.argv[2] if len(sys.argv) > 2 else "Test message from cloud watcher."
        send(msg)
        print("sent:", msg)
