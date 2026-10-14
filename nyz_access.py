"""
nyz_access.py — paid access gate for NYZTrade Streamlit apps.

Same design as pyqoo_access.py. Differences are deliberate and listed here:

  * pro_match_text is REQUIRED, not optional. Pyqoo and NYZTrade bill through
    one Razorpay account, so Razorpay confirms a Pyqoo payment as perfectly
    real and paid. Amount cannot separate them either — price two products the
    same and each unlocks the other. The match text is the only real barrier,
    so this module refuses to start without it.

  * Device rows are namespaced with a product prefix (DEVICE_PREFIX) so one
    Supabase table serves every product without a new project.

  * Supabase auth sends the new-style sb_secret_ keys in the apikey header
    only. Legacy JWT keys (eyJ...) additionally get an Authorization: Bearer
    header, because those still need it.

Everything else follows Pyqoo: Razorpay verification, a 10-minute recheck via
st.cache_data(ttl=600) so refunds take effect, an HMAC-signed token kept in
browser localStorage through a hidden runtime component (never in the URL),
comp passes, and a "Licensed to" watermark.
"""

import base64
import hashlib
import hmac
import html as _html
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components

IST = timezone(timedelta(hours=5, minutes=30))
RECHECK_TTL = 600          # 10 minutes, as Pyqoo
HTTP_TIMEOUT = 12
STORE_KEY = "nyz_auth_v1"  # localStorage key
DEVICE_PREFIX = "nyzval"   # namespaces this product's rows in the shared table


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def _sec(key, default=None):
    try:
        return st.secrets[key]
    except Exception:
        return default


def _fail(msg):
    st.error(msg)
    st.stop()


def config():
    c = {
        "key_id": _sec("razorpay_key_id"),
        "key_secret": _sec("razorpay_key_secret"),
        "pay_url": _sec("pay_url"),
        # Free valuations per calendar month, per email. 0 turns the trial off.
        "trial_runs": int(_sec("trial_runs", 5) or 0),
        "help_url": _sec("help_url", ""),
        "min_rupees": float(_sec("pro_min_amount_rupees", 0) or 0),
        "valid_days": int(_sec("pro_valid_days", 180) or 180),
        "match_text": str(_sec("pro_match_text", "") or "").strip(),
        "session_secret": _sec("session_secret"),
        "product_name": str(_sec("product_name", "NYZTrade Valuation Pro")),
        "supabase_url": str(_sec("supabase_url", "") or "").rstrip("/"),
        "supabase_key": str(_sec("supabase_service_key", "") or ""),
        "max_devices": int(_sec("max_devices", 2) or 2),
        # Overridable so the test harness can point at a mock server.
        "razorpay_api": str(_sec("razorpay_api_base", "https://api.razorpay.com/v1")),
    }

    for k, label in (("key_id", "razorpay_key_id"),
                     ("key_secret", "razorpay_key_secret"),
                     ("pay_url", "pay_url"),
                     ("session_secret", "session_secret")):
        if not c[k]:
            _fail(f"Setup problem: `{label}` is missing from this app's Secrets. "
                  "Add it in Streamlit Cloud under Settings → Secrets.")

    if not c["match_text"]:
        _fail("Setup problem: `pro_match_text` is empty.\n\n"
              "This app shares a Razorpay account with Pyqoo, so without a match "
              "text a Pyqoo payment would unlock this app. Set it to a word that "
              "appears in this product's Payment Page title, e.g. \"NYZTrade\".")

    if c["min_rupees"] <= 0:
        _fail("Setup problem: `pro_min_amount_rupees` must be greater than 0.")

    return c


# ---------------------------------------------------------------------------
# signed token
# ---------------------------------------------------------------------------
def sign(payload, secret):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return body + "." + sig


def unsign(token, secret):
    try:
        body, sig = str(token).split(".", 1)
    except Exception:
        return None
    want = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, want):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# hidden localStorage component, written at runtime
# ---------------------------------------------------------------------------
_STORE_HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;height:0;overflow:hidden">
<script>
function post(t, extra){
  var m = {isStreamlitMessage:true, type:t};
  if (extra) { for (var k in extra) { m[k] = extra[k]; } }
  window.parent.postMessage(m, "*");
}
function send(v){ post("streamlit:setComponentValue", {value:v}); }

var KEY = "__KEY__";

function read(){
  var tok = null, dev = null;
  try { tok = window.localStorage.getItem(KEY); } catch (e) { tok = null; }
  try { dev = window.localStorage.getItem(KEY + "_dev"); } catch (e) { dev = null; }
  if (!dev) {
    try {
      dev = "d_" + Math.random().toString(36).slice(2) + Date.now().toString(36);
      window.localStorage.setItem(KEY + "_dev", dev);
    } catch (e) { dev = "d_nostore"; }
  }
  return {token: tok, device: dev};
}

window.addEventListener("message", function(ev){
  if (!ev.data) { return; }
  if (ev.data.type !== "streamlit:render") { return; }
  var args = ev.data.args || {};
  var act = args.action || "get";
  try {
    if (act === "set" && args.token) {
      window.localStorage.setItem(KEY, args.token);
    } else if (act === "clear") {
      window.localStorage.removeItem(KEY);
    }
  } catch (e) { /* private mode: session still works, just not remembered */ }
  post("streamlit:setFrameHeight", {height:0});
  send(read());
});

post("streamlit:componentReady", {apiVersion:1});
post("streamlit:setFrameHeight", {height:0});
</script></body></html>"""


@st.cache_resource(show_spinner=False)
def _store_component():
    """
    Write the component to disk once per container and declare it.

    Kept inside the app rather than shipped as a folder so Niyas only ever
    uploads app.py + nyz_access.py through the GitHub website.
    """
    base = Path(os.environ.get("NYZ_STORE_DIR", tempfile.gettempdir())) / "nyz_store"
    base.mkdir(parents=True, exist_ok=True)
    (base / "index.html").write_text(_STORE_HTML.replace("__KEY__", STORE_KEY),
                                     encoding="utf-8")
    return components.declare_component("nyz_store", path=str(base))


def _hide_frame():
    """
    The component renders an iframe. Without this it leaves a tall blank gap
    above the app, because Streamlit reserves space for the element container.
    """
    st.markdown("""
    <style>
      div[data-testid="stElementContainer"]:has(iframe[title$="nyz_store"]),
      div[data-testid="element-container"]:has(iframe[title$="nyz_store"]) {
        height: 0 !important; min-height: 0 !important;
        margin: 0 !important; padding: 0 !important;
        overflow: hidden !important; visibility: hidden !important;
      }
      iframe[title$="nyz_store"] { height: 0 !important; display: block !important; }
    </style>
    """, unsafe_allow_html=True)


def sync_store():
    """
    Talk to localStorage. Called EXACTLY ONCE per script run.

    Two things learned the hard way here:

    1. A Streamlit component only acts when it renders. Calling it and then
       immediately st.rerun() throws the render away, so the token never
       reaches localStorage and the buyer is logged out on refresh.
       The write therefore happens on a run that does NOT rerun.

    2. Calling the component twice in one run (once to read, once to write)
       is a duplicate-key error. So there is one call site, and the action is
       derived from session state.

    While signed in, the token is re-asserted on every run. That is idempotent
    and self-heals a write that was lost to a reload mid-flight.
    """
    _hide_frame()
    SS = st.session_state

    if SS.pop("nyz_do_clear", False):
        action, tok = "clear", None
        SS["nyz_signed_out"] = True
    elif SS.get("nyz_token"):
        action, tok = "set", SS["nyz_token"]
    else:
        action, tok = "get", None

    try:
        comp = _store_component()
        state = comp(action=action, token=tok, key="nyz_store_ui", default=None)
    except Exception:
        state = None

    # Remember the device id so later code never needs a second component call.
    if state and state.get("device"):
        SS["nyz_device"] = state["device"]

    # A component returns its previous value until the new one round-trips, so
    # straight after a sign-out it still reports the old token. Trusting that
    # would sign the user back in immediately. Ignore the stored token until
    # the component confirms it is gone.
    if SS.get("nyz_signed_out"):
        if state is not None and not state.get("token"):
            SS.pop("nyz_signed_out", None)
    return state


# ---------------------------------------------------------------------------
# Razorpay
# ---------------------------------------------------------------------------
@st.cache_data(ttl=RECHECK_TTL, show_spinner=False)
def fetch_payment(payment_id, key_id, key_secret, api_base):
    """
    Ask Razorpay about one payment. Cached 10 minutes, so a refund or an
    expiry takes effect within that window without any webhook.
    Returns (data, error).
    """
    pid = str(payment_id or "").strip()
    if not pid.startswith("pay_"):
        return None, "That does not look like a Payment ID — it starts with pay_"
    try:
        r = requests.get(f"{api_base}/payments/{pid}",
                         auth=(key_id, key_secret), timeout=HTTP_TIMEOUT)
    except Exception:
        return None, "Could not reach Razorpay just now. Please try again shortly."
    if r.status_code in (400, 404):
        return None, ("We could not find that payment. If you paid in live mode, "
                      "make sure this app is using live keys.")
    if r.status_code == 401:
        return None, "This app's Razorpay keys are not working. Please contact support."
    if r.status_code != 200:
        return None, f"Razorpay returned an unexpected response ({r.status_code})."
    try:
        return r.json(), None
    except Exception:
        return None, "Razorpay sent a response this app could not read."


def _product_text(p):
    """Every field that could carry the product name."""
    bits = [str(p.get("description") or "")]
    notes = p.get("notes")
    if isinstance(notes, dict):
        for k, v in notes.items():
            bits.append(str(k))
            bits.append(str(v))
    elif notes:
        bits.append(str(notes))
    return " ".join(bits).lower()


def validate(payment, cfg, typed_email):
    """Returns (ok, reason, info)."""
    p = payment or {}

    if p.get("status") != "captured":
        s = p.get("status", "unknown")
        if s == "authorized":
            return False, ("That payment was started but not completed. If money "
                           "left your account it is returned automatically."), {}
        if s == "failed":
            return False, "That payment failed, so no access could be granted.", {}
        return False, f"That payment is not complete (status: {s}).", {}

    if int(p.get("amount_refunded") or 0) > 0 or p.get("refund_status") in ("partial", "full"):
        return False, "That payment has been refunded, so access has ended.", {}

    paid = int(p.get("amount") or 0) / 100.0
    if paid + 0.01 < cfg["min_rupees"]:
        return False, (f"That payment was ₹{paid:,.0f}, below the ₹{cfg['min_rupees']:,.0f} "
                       f"needed for {cfg['product_name']}."), {}

    # the Pyqoo/NYZTrade separation
    if cfg["match_text"].lower() not in _product_text(p):
        return False, (f"That is a real payment, but it was not for {cfg['product_name']}. "
                       "Please use the Payment ID from this product's purchase."), {}

    paid_email = str(p.get("email") or "").strip().lower()
    want = str(typed_email or "").strip().lower()
    if not want:
        return False, "Please enter the email address you paid with.", {}
    if paid_email and paid_email != want:
        return False, ("That email does not match the one on the payment. Use the "
                       "address you entered on the payment page."), {}

    created = int(p.get("created_at") or 0) or int(time.time())
    expires = created + cfg["valid_days"] * 86400
    if time.time() > expires:
        return False, (f"That purchase has expired. Access lasts {cfg['valid_days']} "
                       "days from the date of payment."), {}

    return True, "", {
        "ref": p.get("id"),
        "email": paid_email or want,
        "amount": paid,
        "created_at": created,
        "expires_at": expires,
        "comp": False,
    }


# ---------------------------------------------------------------------------
# Supabase device limit
# ---------------------------------------------------------------------------
def _sb_headers(cfg):
    """
    New-style keys (sb_secret_…) go in apikey only — sending them as a Bearer
    token makes Supabase reject the request. Legacy JWT keys still need both.
    """
    key = cfg["supabase_key"]
    h = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):
        h["Authorization"] = f"Bearer {key}"
    return h


def _ref(payment_ref):
    return f"{DEVICE_PREFIX}:{payment_ref}"


def device_check(cfg, payment_ref, device, email):
    """
    Allow this device if it is already known, or if the payment is under its
    device limit. Fails OPEN on any database problem: a Supabase outage must
    not lock out a paying customer.
    Returns (allowed, used_count, message).
    """
    if not cfg["supabase_url"] or not cfg["supabase_key"]:
        return True, 0, ""
    if not device or device == "d_nostore":
        return True, 0, ""

    ref = _ref(payment_ref)
    base = f"{cfg['supabase_url']}/rest/v1/pyqoo_devices"
    try:
        r = requests.get(base, headers=_sb_headers(cfg), timeout=HTTP_TIMEOUT,
                         params={"ref": f"eq.{ref}", "select": "device"})
        if r.status_code != 200:
            return True, 0, ""
        rows = r.json() or []
    except Exception:
        return True, 0, ""

    known = {str(x.get("device")) for x in rows}
    if device in known:
        return True, len(known), ""

    if len(known) >= cfg["max_devices"]:
        return False, len(known), (
            f"This purchase is already in use on {len(known)} devices, which is the "
            f"limit of {cfg['max_devices']}. Sign out on another device, or contact "
            "support to reset it.")

    try:
        requests.post(base, headers=_sb_headers(cfg), timeout=HTTP_TIMEOUT,
                      json={"ref": ref, "device": device, "email": email})
    except Exception:
        pass
    return True, len(known) + 1, ""


# ---------------------------------------------------------------------------
# Free trial
#
# Keyed on the EMAIL, not the device. The device id is minted in the browser
# ("d_" + a random number in localStorage), so a device-keyed trial restarts
# the moment someone clears site data. An email is the smallest thing the
# person carries between browsers.
#
# No new table: each use is one row in pyqoo_devices under a trial ref, so the
# number of rows IS the number of valuations used. Counting rows server-side
# means the tally cannot be edited from the browser.
#
# The ref carries the month, so the allowance refills on the 1st without any
# scheduled job: a new month simply looks under a key that has no rows yet.
#
# A throwaway email still gets a fresh trial. That is the residual hole, and
# closing it needs emailed one-time codes, which is a bigger build.
# ---------------------------------------------------------------------------
TRIAL_PREFIX = "nyzvaltrial:"


def trial_month(when=None):
    """Current month in IST, so the reset lands at midnight on the 1st for
    Indian users rather than part-way through their 1st."""
    now = when or datetime.now(IST)
    return f"{now.year:04d}-{now.month:02d}"


def trial_ref(email, month=None):
    """The month is part of the key, so a new month simply finds no rows and
    the allowance starts again. Nothing has to run on a schedule to reset it,
    and last month's rows stay as a record of who used what."""
    return (TRIAL_PREFIX + (month or trial_month()) + ":"
            + (email or "").strip().lower())


def trial_used(cfg, email):
    """Valuations already spent, or None when the database cannot be reached."""
    if not cfg.get("supabase_url"):
        return None
    base = f"{cfg['supabase_url']}/rest/v1/pyqoo_devices"
    try:
        r = requests.get(base, headers=_sb_headers(cfg), timeout=6,
                         params={"ref": f"eq.{trial_ref(email)}", "select": "device"})
        if r.status_code >= 300:
            return None
        return len(r.json() or [])
    except Exception:
        return None


def trial_consume(cfg, email, device=""):
    """Record one used valuation. False only when the write itself failed."""
    if not cfg.get("supabase_url"):
        return False
    base = f"{cfg['supabase_url']}/rest/v1/pyqoo_devices"
    try:
        r = requests.post(base, headers=_sb_headers(cfg), timeout=6,
                          json={"ref": trial_ref(email),
                                "device": uuid.uuid4().hex,
                                "email": (email or "").strip().lower()})
        return r.status_code < 300
    except Exception:
        return False


def trial_left(cfg, email):
    """How many runs remain. None means 'cannot tell' — callers allow the run."""
    cap = int(cfg.get("trial_runs") or 0)
    if cap <= 0:
        return 0
    used = trial_used(cfg, email)
    if used is None:
        return None
    return max(0, cap - used)


def device_forget(cfg, payment_ref, device):
    """Remove this device on sign-out, freeing a slot."""
    if not cfg["supabase_url"] or not cfg["supabase_key"] or not device:
        return
    try:
        requests.delete(f"{cfg['supabase_url']}/rest/v1/pyqoo_devices",
                        headers=_sb_headers(cfg), timeout=HTTP_TIMEOUT,
                        params={"ref": f"eq.{_ref(payment_ref)}",
                                "device": f"eq.{device}"})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# comp passes
# ---------------------------------------------------------------------------
def comp_ok(email, word):
    """[comp_access] is  "email" = "secret word"."""
    e = str(email or "").strip().lower()
    w = str(word or "").strip()
    if not e or not w:
        return False
    try:
        table = dict(st.secrets["comp_access"])
    except Exception:
        return False
    for k, v in table.items():
        if str(k).strip().lower() == e and str(v).strip() == w:
            return True
    return False


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def _qp(name):
    try:
        v = st.query_params.get(name)
    except Exception:
        try:
            v = st.experimental_get_query_params().get(name)
        except Exception:
            v = None
    if isinstance(v, list):
        v = v[0] if v else None
    return v


def require_access():
    """Call first in app.py. Returns the buyer's info, or renders the gate and stops."""
    cfg = config()
    SS = st.session_state

    state = sync_store()
    device = (state or {}).get("device") or SS.get("nyz_device")
    saved = None if SS.get("nyz_signed_out") else (state or {}).get("token")

    # The component answers on the second run. Wait for it once rather than
    # showing the buy screen to someone who is already signed in.
    if state is None and not SS.get("nyz_waited"):
        SS["nyz_waited"] = True
        st.stop()

    token = SS.get("nyz_token") or saved
    if token:
        data = unsign(token, cfg["session_secret"])
        if data and time.time() < data.get("expires_at", 0):
            if data.get("comp"):
                SS["nyz_token"] = token
                return data
            if data.get("trial"):
                # No payment to verify. The cap is enforced per valuation in
                # the app, against Supabase, not here.
                SS["nyz_token"] = token
                return data
            p, err = fetch_payment(data.get("ref"), cfg["key_id"],
                                   cfg["key_secret"], cfg["razorpay_api"])
            if p:
                ok, reason, _ = validate(p, cfg, data.get("email"))
                if ok:
                    SS["nyz_token"] = token
                    return data
                SS.pop("nyz_token", None)
                SS["nyz_do_clear"] = True
                st.warning(reason)
            else:
                # Razorpay unreachable — do not evict a paying customer over a blip
                SS["nyz_token"] = token
                return data
        else:
            SS.pop("nyz_token", None)

    _gate(cfg, device, _qp("razorpay_payment_id"))
    st.stop()


def _grant(cfg, info):
    """
    Put the token in session state and rerun. The next run calls sync_store()
    with action="set", which is the render that actually writes localStorage.
    Writing here instead would be discarded by the rerun below.
    """
    st.session_state.pop("nyz_signed_out", None)
    st.session_state["nyz_token"] = sign(info, cfg["session_secret"])
    st.rerun()


# ---------------------------------------------------------------------------
# Shared look. BASE_CSS is injected on every screen; the gate and the
# worksheet each add their own layer on top. Keeping the tokens in one place
# is why the paid screen and the calculator look like the same product.
# ---------------------------------------------------------------------------
# Every colour in both screens comes from these tokens. The dark set simply
# redefines them, which is why one toggle can repaint the whole app including
# Streamlit's own widgets.
LIGHT_VARS = """
  --bg:#FFFFFF; --s1:#FFFFFF; --s2:#FAF8FF; --s3:#F6F3FE;
  --bd:#E4DEF7; --bd2:#D9D0F0;
  --ink:#1B1033; --ink2:#4A3D6B; --ink3:#6B5E8A;
  --brand:#6D28D9; --brand2:#8B5CF6; --onbrand:#FFFFFF; --brandink:#4C1D95;
  --gain:#0F7A52; --loss:#B4233A;
  --gainfill:#A7F3D0; --lossfill:#FECDD3;
  --shadow:rgba(76,29,149,.42); --glow:rgba(109,40,217,.55); --logo:#6D28D9;
"""

DARK_VARS = """
  --bg:#130D22; --s1:#1B1333; --s2:#211841; --s3:#281D4C;
  --bd:#3A2E63; --bd2:#4C3F80;
  --ink:#F4F1FD; --ink2:#D2C8EC; --ink3:#AEA1D4;
  --brand:#7C3AED; --brand2:#A78BFA; --onbrand:#FFFFFF; --brandink:#C4B5FD;
  --gain:#34D399; --loss:#FB7185;
  --gainfill:#14532D; --lossfill:#4C1D24;
  --shadow:rgba(0,0,0,.65); --glow:rgba(124,58,237,.55); --logo:#C4B5FD;
"""


def _base_css(dark):
    """Shared look. Tokens live on :root so BaseWeb's portalled dropdown
    menus — which render outside .stApp — can read them too."""
    return """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,700&family=Fraunces:opsz,wght@9..144,600;9..144,700&display=swap');

:root{""" + (DARK_VARS if dark else LIGHT_VARS) + """}

/* Bumped from the browser default of 16px. Every size in this app is in rem,
   so this one line scales the whole interface rather than a list of tweaks. */
html{font-size:17.5px;}

/* Streamlit's chrome is noise on a product screen. */
#MainMenu, footer, header [data-testid="stStatusWidget"] {visibility:hidden;}
[data-testid="stToolbar"], [data-testid="stDecoration"] {display:none !important;}

/* The canvas is pinned to our own token, never left to Streamlit's theme:
   without this a visitor in dark mode got a near-black page under text
   coloured for a light one. */
.stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"],
[data-testid="stHeader"], body {background:var(--bg) !important;}
[data-testid="stSidebarContent"]{background:var(--s1) !important;}

/* Font is set on text elements only, never on bare spans. Streamlit draws its
   icons as ligature text inside spans, so a blanket span rule would replace
   the arrow glyph with the literal word "keyboard_arrow_right". */
.stApp, .stApp p, .stApp label, .stApp li {
  font-family:'DM Sans',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  color:var(--ink);
}
.stApp p, .stApp li{font-size:1rem;}
/* Headings and captions follow Streamlit's own theme colour, which is fixed
   by config.toml and so stayed dark-on-dark in the sidebar. Links too: an
   email in a caption is auto-linkified and came out in default link blue. */
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6{
  color:var(--ink) !important;
}
.stApp [data-testid="stCaptionContainer"],
.stApp [data-testid="stCaptionContainer"] *{color:var(--ink3) !important;}
.stApp a:not([data-testid*="LinkButton"]):not([kind]){color:var(--brandink) !important;}

/* Figures line up column-wise only with tabular figures. On a valuation
   sheet every number is meant to be compared with the one above it. */
.nyznum, .nyzfig, table.nyztbl td, table.nyztbl th {font-variant-numeric:tabular-nums;}

/* ---- buttons ---- */
[data-testid="stLinkButton"] a, [data-testid="stBaseLinkButton-primary"],
[data-testid="stBaseLinkButton-secondary"]{
  border-radius:11px !important; padding:13px 18px !important;
  font-weight:700 !important; font-size:1rem !important; border-width:1px !important;
}
/* ^= prefix, not = exact. A form's submit button is "stBaseButton-primary
   FormSubmit", a plain one is "stBaseButton-primary"; an exact selector
   styles only the second and leaves the Unlock button Streamlit red. */
[data-testid^="stBaseButton-primary"]{
  border-radius:11px !important; padding:13px 18px !important; font-weight:700 !important;
  font-size:1rem !important; border:none !important;
  box-shadow:0 10px 26px -12px var(--glow) !important;
}
[data-testid^="stBaseButton-primary"]:hover{filter:brightness(1.08);}

/* The ' *' half matters. Streamlit wraps button text in its own <p>, and the
   base ".stApp p" colour above would otherwise win there, leaving dark text
   on the purple fill at 2.5:1. Colour the descendants, not just the button. */
[data-testid^="stBaseButton-primary"],
[data-testid^="stBaseButton-primary"] *,
[data-testid^="stBaseLinkButton-primary"],
[data-testid^="stBaseLinkButton-primary"] *,
[data-testid="stLinkButton"] a[kind="primary"],
[data-testid="stLinkButton"] a[kind="primary"] *{
  color:var(--onbrand) !important;
}
[data-testid^="stBaseButton-primary"],
[data-testid^="stBaseLinkButton-primary"],
[data-testid="stLinkButton"] a[kind="primary"]{
  /* border-color too: Streamlit outlines link buttons in primaryColor, which
     left a thin red ring around the purple Buy button. */
  background:var(--brand) !important; border-color:var(--brand) !important;
}
[data-testid^="stBaseButton-secondary"],
[data-testid^="stBaseLinkButton-secondary"],
[data-testid="stLinkButton"] a[kind="secondary"]{
  border-radius:11px !important; font-weight:600 !important;
  background:var(--s1) !important; border:1px solid var(--bd2) !important;
}
[data-testid^="stBaseButton-secondary"],
[data-testid^="stBaseButton-secondary"] *,
[data-testid^="stBaseLinkButton-secondary"],
[data-testid^="stBaseLinkButton-secondary"] *,
[data-testid="stLinkButton"] a[kind="secondary"],
[data-testid="stLinkButton"] a[kind="secondary"] *{
  color:var(--brandink) !important;
}

/* ---- inputs ---- */
[data-testid="stTextInputRootElement"], [data-testid="stNumberInputContainer"]{
  border-radius:10px !important; border:1px solid var(--bd) !important;
  background:var(--s2) !important;
}
[data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stNumberInputContainer"]:focus-within{
  border-color:var(--brand) !important;
  box-shadow:0 0 0 3px color-mix(in srgb, var(--brand) 18%, transparent) !important;
}
[data-testid="stTextInputRootElement"] input,
[data-testid="stNumberInputContainer"] input{
  background:transparent !important; color:var(--ink) !important; font-size:1rem !important;
}
[data-testid="stNumberInputContainer"] input{font-variant-numeric:tabular-nums;}
/* Browsers fade placeholders by default, which on the dark canvas left
   "e.g. Infosys" almost unreadable. Set the colour and cancel the fade. */
.stApp input::placeholder{color:var(--ink3) !important;opacity:1 !important;}
/* The steppers stay (they are useful on a phone) but stop competing with the
   figure itself. */
[data-testid="stNumberInputStepUp"], [data-testid="stNumberInputStepDown"]{
  background:transparent !important; color:var(--ink3) !important; border:none !important;
}
[data-testid="stNumberInputStepUp"]:hover, [data-testid="stNumberInputStepDown"]:hover{
  color:var(--brand2) !important; background:var(--s3) !important;
}
.stTextInput label, .stNumberInput label, .stSelectbox label, .stToggle label{
  font-size:.95rem !important; font-weight:500 !important; color:var(--ink2) !important;
}

/* Selects are painted by Streamlit itself from config.toml's
   secondaryBackgroundColor, so they stayed light when the app went dark.
   The control is div[role="group"] inside the widget — targeted structurally
   because the only other handle is a hashed emotion class that changes
   between Streamlit releases. */
[data-testid="stSelectbox"] div[role="group"]{
  border-radius:10px !important; border-color:var(--bd) !important;
  background:var(--s2) !important;
}
[data-testid="stSelectbox"] div[role="group"]:focus-within{
  border-color:var(--brand) !important;
  box-shadow:0 0 0 3px color-mix(in srgb, var(--brand) 18%, transparent) !important;
}
[data-testid="stSelectbox"] div[role="group"] *{color:var(--ink) !important;}
[data-testid="stSelectbox"] svg{fill:var(--ink3) !important;}

/* The menu is portalled to <body>, outside .stApp — do not scope these. */
[role="listbox"], [data-baseweb="popover"] ul, [data-baseweb="menu"]{
  background:var(--s1) !important; border:1px solid var(--bd) !important;
}
[role="option"], [role="listbox"] li{color:var(--ink) !important;
  background:var(--s1) !important;}
[role="option"]:hover, [role="listbox"] li:hover,
[role="option"][aria-selected="true"]{background:var(--s3) !important;}

/* ---- expander + alerts ---- */
[data-testid="stExpander"]{border:1px solid var(--bd) !important;border-radius:12px !important;
  background:var(--s1) !important;}
[data-testid="stExpander"] summary{font-size:.95rem !important;color:var(--ink2) !important;}
[data-testid="stAlert"]{border-radius:11px !important;}
</style>
"""


GATE_CSS = """
<style>
[data-testid="stMainBlockContainer"], .block-container {
  max-width:660px !important; padding-top:2.2rem !important; padding-bottom:3rem !important;
}

/* ---- brand header ---- */
.nyzhero{text-align:center;margin-bottom:26px;background:var(--s3);
  border:1px solid var(--bd);border-radius:20px;padding:26px 20px 24px;}
.nyzmark{width:62px;height:62px;margin:0 auto 16px;border-radius:18px;
  background:var(--s1);border:1.5px solid var(--brand);display:flex;align-items:center;
  justify-content:center;color:var(--logo);}
.nyzname{font-family:'Fraunces',Georgia,serif;font-weight:700;font-size:1.95rem;
  color:var(--brandink);letter-spacing:-.02em;line-height:1.15;margin-bottom:6px;}
.stApp .nyzsub{color:var(--ink2);font-size:1.02rem;max-width:42ch;margin:0 auto;}

/* ---- price band ---- */
.nyzprice{background:linear-gradient(135deg,var(--brand),var(--brand2));border-radius:18px;
  padding:24px 22px;text-align:center;color:#fff;margin:22px 0 10px;
  box-shadow:0 16px 40px -18px var(--glow);}
.nyzamt{font-family:'Fraunces',serif;font-weight:700;font-size:3rem;line-height:1;
  font-variant-numeric:tabular-nums;}
.nyzamt small{font-size:1rem;font-weight:400;opacity:.9;margin-left:7px;
  font-family:'DM Sans',sans-serif;}
.nyzterms{font-size:.92rem;opacity:.95;margin-top:7px;}

/* ---- trust row ---- */
.nyztrust{display:flex;gap:9px;margin:14px 0 18px;flex-wrap:wrap;}
.nyztrust div{flex:1 1 30%;background:var(--s3);border:1px solid var(--bd);
  border-radius:11px;padding:11px 8px;text-align:center;}
.nyztrust b{display:block;font-size:1.08rem;color:var(--brandink);font-weight:700;
  line-height:1.3;}
.nyztrust span{font-size:.8rem;color:var(--ink3);}

/* ---- divider ---- */
.nyzsep{display:flex;align-items:center;gap:13px;margin:30px 0 16px;
  color:var(--ink3);font-size:.92rem;font-weight:600;}
.nyzsep:before,.nyzsep:after{content:"";flex:1;height:1px;background:var(--bd);}

/* ---- form card ---- */
[data-testid="stForm"]{
  background:var(--s1) !important; border:1px solid var(--bd) !important;
  border-radius:16px !important; padding:20px !important;
  box-shadow:0 12px 34px -20px var(--shadow) !important;
}

/* ---- notes. Scoped under .stApp because Streamlit renders these as <p>,
   where its own ".stMarkdown p" rule outranks a bare class selector. ---- */
.stApp .nyzfine{font-size:.88rem;color:var(--ink3);text-align:center;margin-top:16px;
  line-height:1.65;}
.stApp .nyzlegal{font-size:.8rem;color:var(--ink3);text-align:center;margin-top:22px;
  padding-top:16px;border-top:1px solid var(--bd);line-height:1.6;}

@media (max-width:640px){
  .nyzamt{font-size:2.5rem;}
  /* Keep the three stats on one row on a phone. Stacking them pushed the
     Buy button below the fold. min-width:0 lets them shrink instead. */
  .nyztrust{gap:6px;}
  .nyztrust div{flex:1 1 0;min-width:0;padding:9px 4px;}
  .nyztrust b{font-size:.98rem;}
  .nyztrust span{font-size:.68rem;}
}
</style>
"""


def dark_mode():
    """True when the viewer has switched this session to dark."""
    return bool(st.session_state.get("nyz_dark", False))


def theme(mode="app"):
    """Inject the shared look, plus the gate layer when asked for it."""
    st.markdown(_base_css(dark_mode()), unsafe_allow_html=True)
    if mode == "gate":
        st.markdown(GATE_CSS, unsafe_allow_html=True)


# currentColor, not a fixed purple: the mark is nearly invisible on the dark
# canvas otherwise. Its container sets color from the --logo token.
LOGO_SVG = """<svg viewBox="0 0 48 48" fill="none" width="32" height="32">
  <path d="M13 34V14l12 13.5V14" stroke="currentColor" stroke-width="3.8"
        stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="30" y="20" width="4" height="14" rx="1.6" fill="currentColor"
        opacity=".78"/>
  <rect x="36.5" y="25" width="4" height="9" rx="1.6" fill="currentColor"
        opacity=".5"/>
</svg>"""


def _gate(cfg, device, auto_id):
    """
    The purchase screen. This is the first thing a buyer sees after paying, so
    it is styled to match the landing page rather than left on Streamlit's
    defaults — an unbranded red-button screen after a payment reads as though
    the money went somewhere else.
    """
    theme("gate")

    st.markdown(
        f'<div class="nyzhero"><div class="nyzmark">{LOGO_SVG}</div>'
        f'<div class="nyzname">{cfg["product_name"]}</div>'
        f'<p class="nyzsub">Value any NSE or BSE stock five ways, against benchmark '
        f'multiples for 28 Indian industries.</p></div>',
        unsafe_allow_html=True)

    st.markdown(
        f'<div class="nyzprice"><div class="nyzamt">\u20B9{cfg["min_rupees"]:,.0f}'
        f'<small>one time</small></div>'
        f'<div class="nyzterms">Yours for {cfg["valid_days"]} days on up to '
        f'{cfg["max_devices"]} devices. Nothing renews by itself.</div></div>',
        unsafe_allow_html=True)

    st.markdown(
        '<div class="nyztrust">'
        '<div><b>5</b><span>Methods</span></div>'
        '<div><b>28</b><span>Industries</span></div>'
        '<div><b>Instant</b><span>Unlock</span></div>'
        '</div>', unsafe_allow_html=True)

    a, b = st.columns([2, 1])
    with a:
        st.link_button(f"Buy for \u20B9{cfg['min_rupees']:,.0f}", cfg["pay_url"],
                       use_container_width=True, type="primary")
    with b:
        if cfg["help_url"]:
            st.link_button("Need help?", cfg["help_url"], use_container_width=True)

    st.markdown('<div class="nyzsep">Already paid</div>', unsafe_allow_html=True)

    with st.form("nyz_unlock"):
        email = st.text_input("Email you paid with", placeholder="you@example.com")
        pid = st.text_input("Payment ID", value=auto_id or "",
                            placeholder="pay_XXXXXXXXXXXX")
        go = st.form_submit_button("Unlock my access", type="primary",
                                   use_container_width=True)

    if cfg.get("trial_runs", 0) > 0:
        with st.expander(f"Try it free — {cfg['trial_runs']} valuations a month"):
            with st.form("nyz_trial"):
                te = st.text_input("Your email", placeholder="you@example.com",
                                   key="nyz_trial_email")
                tgo = st.form_submit_button("Start my free valuations",
                                            use_container_width=True)
            st.caption("No payment, no card. The count is kept against your email, "
                       "so it follows you between browsers, and it refills on the "
                       "1st of each month.")
    else:
        te, tgo = "", False

    with st.expander("I have a free pass"):
        with st.form("nyz_comp"):
            ce = st.text_input("Email")
            cw = st.text_input("Secret word", type="password")
            cgo = st.form_submit_button("Use free pass", use_container_width=True)

    st.markdown(
        '<p class="nyzfine">Your Payment ID starts with <b>pay_</b> and is in the '
        'confirmation email from Razorpay. Paying through the button above brings '
        'you straight back here and unlocks the tool automatically.</p>',
        unsafe_allow_html=True)

    st.markdown(
        '<p class="nyzlegal">Educational tool. It performs arithmetic on figures you '
        'supply and does not verify them or fetch market data. Not investment advice, '
        'not a recommendation, not a price target. NYZTrade is not a SEBI-registered '
        'investment adviser.</p>', unsafe_allow_html=True)

    if tgo:
        addr = (te or "").strip().lower()
        if "@" not in addr or "." not in addr.split("@")[-1]:
            st.error("Please enter an email address so the free runs can be counted.")
        else:
            left = trial_left(cfg, addr)
            if left == 0:
                st.error(
                    f"This email has used all {cfg['trial_runs']} free valuations "
                    "for this month. They refill on the 1st, or buying below "
                    "removes the limit now.")
            else:
                now = int(time.time())
                _grant(cfg, {"ref": trial_ref(addr), "email": addr, "amount": 0,
                             "created_at": now, "expires_at": now + 365 * 86400,
                             "comp": False, "trial": True})

    if cgo:
        if comp_ok(ce, cw):
            now = int(time.time())
            _grant(cfg, {"ref": "comp", "email": ce.strip().lower(), "amount": 0,
                         "created_at": now, "expires_at": now + cfg["valid_days"] * 86400,
                         "comp": True})
        else:
            st.error("That free pass was not recognised.")

    if go:
        p, err = fetch_payment(pid.strip(), cfg["key_id"], cfg["key_secret"],
                               cfg["razorpay_api"])
        if err:
            st.error(err)
            return
        ok, reason, info = validate(p, cfg, email)
        if not ok:
            st.error(reason)
            return
        allowed, used, msg = device_check(cfg, info["ref"], device, info["email"])
        if not allowed:
            st.error(msg)
            return
        _grant(cfg, info)


def _account_lines(data, cfg):
    """The one description of an account, used by every place that shows it."""
    out = []
    exp = data.get("expires_at")
    if data.get("trial"):
        left = trial_left(cfg, data.get("email"))
        out.append("Free trial" if left is None
                   else f"Free trial \u2014 {left} of {cfg['trial_runs']} "
                        "valuations left this month")
    elif data.get("comp"):
        out.append("Free pass")
    elif exp:
        days = int((exp - time.time()) / 86400)
        out.append(f"Access until {datetime.fromtimestamp(exp, IST):%d %b %Y} "
                   f"({days} days left)")
    return out


def sign_out(cfg, data):
    """Drop this session. A paid sign-out also frees the device slot, which is
    how someone moves the purchase to a new machine; a trial or comp pass never
    took a slot, so there is nothing to give back."""
    if not data.get("comp") and not data.get("trial"):
        device_forget(cfg, data.get("ref"), st.session_state.get("nyz_device"))
    st.session_state.pop("nyz_token", None)
    st.session_state["nyz_do_clear"] = True
    st.rerun()


def account_menu(data, cfg=None):
    """Account and sign-out, for the top of the page.

    The sidebar starts collapsed, so the sign-out button that lived only in
    there was effectively hidden. This renders the same thing where it can
    actually be seen.
    """
    if not data:
        return
    cfg = cfg or config()
    email = data.get("email", "")

    # st.popover keeps the header to one control instead of a loose button,
    # and makes signing out deliberate rather than a stray click. Older
    # Streamlit versions fall back to a plain button.
    if hasattr(st, "popover"):
        with st.popover("Account", use_container_width=True):
            # Plain text, not markdown: Streamlit turns a bare address into a
            # mailto link, which looks clickable and does nothing useful here.
            st.markdown(
                f'<div style="font-weight:700;word-break:break-all">'
                f'{_html.escape(email)}</div>', unsafe_allow_html=True)
            for line in _account_lines(data, cfg):
                st.caption(line)
            if not data.get("comp") and not data.get("trial"):
                st.caption("Signing out frees this device, so you can use the "
                           "purchase somewhere else.")
            if st.button("Sign out", use_container_width=True, key="nyz_out_hdr"):
                sign_out(cfg, data)
    else:
        if st.button("Sign out", use_container_width=True, key="nyz_out_hdr"):
            sign_out(cfg, data)


def sidebar(data, cfg=None):
    """Account box plus the licence watermark."""
    if not data:
        return
    cfg = cfg or config()
    with st.sidebar:
        st.markdown("### Account")
        st.caption(data.get("email", ""))
        for line in _account_lines(data, cfg):
            st.caption(line)
        # No sign-out button here. It lives in the header now, and two controls
        # with the same label is one more thing to keep in step.
        st.caption("Sign out from the Account menu at the top of the page.")

    st.markdown(
        # No negative margin: it used to pull this line into the container's
        # top padding, where it was clipped mid-glyph.
        f"<div style='text-align:right;font-size:.82rem;color:var(--ink3);"
        f"margin:0 0 2px'>Licensed to {data.get('email','')}</div>",
        unsafe_allow_html=True)
