#!/usr/bin/env python3
"""ERP auto-login: fills creds + security answer, triggers OTP, reads the NEW
OTP from Gmail, authenticates. ALL credentials -- including security answers --
come from environment variables. Nothing sensitive is hardcoded in this file,
since this file lives in a PUBLIC repo. Returns a logged-in session cookie
string. Run directly to self-test.

Required env vars:
  ERP_USER, ERP_PW      -- ERP login id + password
  GMAIL, APP_PW         -- Gmail address + app password (for OTP retrieval)
  SECURITY_ANSWERS      -- "keyword1=answer1,keyword2=answer2,..." -- one
                           entry per security question you have set up on the ERP
  OTP_SENDER (optional) -- defaults to erpkgp@adm.iitkgp.ac.in
"""
import os, re, time, imaplib
import requests

USER = os.environ.get("ERP_USER", "")
PW = os.environ.get("ERP_PW", "")
GMAIL = os.environ.get("GMAIL", "")
APP_PW = os.environ.get("APP_PW", "")
OTP_SENDER = os.environ.get("OTP_SENDER", "erpkgp@adm.iitkgp.ac.in")
SSO = "https://erp.iitkgp.ac.in/SSOAdministration"

def _load_answers():
    raw = os.environ.get("SECURITY_ANSWERS", "")
    out = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out

ANSWERS = _load_answers()

def _pick(q):
    ql = (q or "").lower()
    for k, v in ANSWERS.items():
        if k in ql:
            return v
    return None

def _newest_otp():
    M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=25)
    try:
        M.login(GMAIL, APP_PW)
        M.select("INBOX")
        typ, d = M.search(None, "FROM", OTP_SENDER)
        ids = d[0].split()
        if not ids:
            return None
        typ, md = M.fetch(ids[-1], "(BODY[HEADER.FIELDS (SUBJECT)])")
        m = re.search(r"is\s+(\d{4,8})", md[0][1].decode("utf-8", "ignore"))
        return m.group(1) if m else None
    finally:
        try: M.logout()
        except Exception: pass

def _wait_new_otp(baseline, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            o = _newest_otp()
            if o and o != baseline:
                return o
        except Exception as e:
            print("imap retry:", e)
        time.sleep(4)
    return None

def _delete_otp_emails(verbose=False):
    """Clears every OTP mail from the inbox once the login cycle is done, so the
    account never accumulates OTP clutter (they're single-use and worthless
    afterwards)."""
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=25)
        try:
            M.login(GMAIL, APP_PW)
            M.select("INBOX")
            typ, d = M.search(None, "FROM", OTP_SENDER)
            ids = d[0].split()
            for i in ids:
                M.store(i, "+FLAGS", "\\Deleted")
            if ids:
                M.expunge()
            if verbose: print("7) OTP emails deleted:", len(ids))
        finally:
            try: M.logout()
            except Exception: pass
    except Exception as e:
        if verbose: print("OTP cleanup failed (non-fatal):", e)

def login(verbose=False):
    if not (USER and PW and GMAIL and APP_PW and ANSWERS):
        if verbose: print("missing required env vars -- see module docstring")
        return None
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    r = s.get("https://erp.iitkgp.ac.in/IIT_ERP3/", timeout=30)
    tok = re.search(r'name="sessionToken"[^>]*value="([^"]+)"', r.text)
    token = tok.group(1) if tok else ""
    if verbose: print("1) session token received:", bool(token))
    q = s.post(f"{SSO}/getSecurityQues.htm", data={"user_id": USER}, timeout=30).text.strip()
    ans = _pick(q)
    if verbose: print("2) security answer matched:", bool(ans))
    baseline = None
    try: baseline = _newest_otp()
    except Exception as e: print("baseline read failed:", e)
    form = {"user_id": USER, "password": PW, "answer": ans, "email_otp": "",
            "sessionToken": token, "requestedUrl": "https://erp.iitkgp.ac.in/IIT_ERP3/", "typeee": "SI"}
    o = s.post(f"{SSO}/getEmilOTP.htm", data=form, timeout=30)
    if verbose: print("3) OTP request:", o.status_code)
    otp = _wait_new_otp(baseline, timeout=150)
    if not otp:
        if verbose: print("   no OTP within 150s -- resending once...")
        o2 = s.post(f"{SSO}/getEmilOTP.htm", data=form, timeout=30)
        if verbose: print("3b) OTP resend:", o2.status_code)
        otp = _wait_new_otp(baseline, timeout=150)
    if verbose: print("4) OTP received:", bool(otp))
    if not otp:
        return None
    form["email_otp"] = otp
    a = s.post(f"{SSO}/auth.htm", data=form, timeout=30)
    h = s.get("https://erp.iitkgp.ac.in/IIT_ERP3/home.htm", timeout=30).text
    ok = ("Welcome" in h) and ("loginForm" not in h)
    if verbose: print("5) LOGGED IN:", ok)
    if not ok:
        if verbose:
            conflict = any(word in (a.text + h).lower() for word in ('already logged', 'abnormal', 'active session'))
            print("   ERP rejected login; existing-session conflict:", conflict)
        _delete_otp_emails(verbose)
        return None
    _delete_otp_emails(verbose)
    return "; ".join(f"{c.name}={c.value}" for c in s.cookies)

def logout(cookie):
    """Frees the ERP session when a cycle is done. The ERP is single-session per
    user, so a session left open can block the next automated (or manual) login."""
    try:
        requests.get("https://erp.iitkgp.ac.in/IIT_ERP3/logout.htm", timeout=20,
                     headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"})
    except Exception:
        pass


if __name__ == "__main__":
    ck = login(verbose=True)
    print("6) cookie chars:", len(ck) if ck else 0)
