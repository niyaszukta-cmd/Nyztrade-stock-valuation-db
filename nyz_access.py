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
BASE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,700&family=Fraunces:opsz,wght@9..144,600;9..144,700&display=swap');

/* Streamlit's chrome is noise on a product screen. */
#MainMenu, footer, header [data-testid="stStatusWidget"] {visibility:hidden;}
[data-testid="stToolbar"], [data-testid="stDecoration"] {display:none !important;}

/* Pin the canvas white. The old selector ("> .main") no longer matches modern
   Streamlit, so a visitor in dark mode got a near-black page under text that
   was coloured for a light one. No single ink passes contrast on both white
   and near-black, so the background is fixed rather than the text. */
.stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"],
[data-testid="stHeader"], body {background:#FFFFFF !important;}

/* Font is set on text elements only, never on bare spans. Streamlit draws its
   icons as ligature text inside spans, so a blanket span rule would replace
   the arrow glyph with the literal word "keyboard_arrow_right". */
.stApp, .stApp p, .stApp label {
  font-family:'DM Sans',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  color:#1B1033;
}

/* Figures line up column-wise only with tabular figures. On a valuation
   sheet every number is meant to be compared against the one above it. */
.nyznum, .nyzfig, table.nyztbl td, table.nyztbl th {font-variant-numeric:tabular-nums;}

/* ---- buttons ---- */
[data-testid="stLinkButton"] a, [data-testid="stBaseLinkButton-primary"],
[data-testid="stBaseLinkButton-secondary"]{
  border-radius:11px !important; padding:13px 18px !important;
  font-weight:700 !important; font-size:.95rem !important; border-width:1px !important;
}
/* ^= prefix, not = exact. A form's submit button is "stBaseButton-primary
   FormSubmit", a plain one is "stBaseButton-primary"; an exact selector
   styles only the second and leaves the Unlock button Streamlit red. */
[data-testid^="stBaseButton-primary"]{
  border-radius:11px !important; padding:13px 18px !important; font-weight:700 !important;
  font-size:.95rem !important; border:none !important;
  box-shadow:0 10px 26px -12px rgba(109,40,217,.7) !important;
}
[data-testid^="stBaseButton-primary"]:hover{filter:brightness(1.07);}

/* Colours are pinned here, not left to the theme. config.toml sets
   primaryColor, but if that file is ever missing Streamlit falls back to its
   default RED and the paid screen looks like a stranger's app. */
/* The ' *' half matters. Streamlit wraps button text in its own <p>, and the
   base ".stApp p" colour above would otherwise win there, leaving dark text
   on the purple fill at 2.5:1. Colour the descendants, not just the button. */
[data-testid^="stBaseButton-primary"],
[data-testid^="stBaseButton-primary"] *,
[data-testid^="stBaseLinkButton-primary"],
[data-testid^="stBaseLinkButton-primary"] *,
[data-testid="stLinkButton"] a[kind="primary"],
[data-testid="stLinkButton"] a[kind="primary"] *{
  color:#FFFFFF !important;
}
[data-testid^="stBaseButton-primary"],
[data-testid^="stBaseLinkButton-primary"],
[data-testid="stLinkButton"] a[kind="primary"]{
  /* border-color too: Streamlit outlines link buttons in primaryColor, which
     left a thin red ring around the purple Buy button. */
  background:#6D28D9 !important; border-color:#6D28D9 !important;
}
[data-testid^="stBaseButton-secondary"],
[data-testid^="stBaseLinkButton-secondary"],
[data-testid="stLinkButton"] a[kind="secondary"]{
  border-radius:11px !important; font-weight:600 !important;
  background:#FFFFFF !important; border:1px solid #C6B9EC !important;
}
[data-testid^="stBaseButton-secondary"],
[data-testid^="stBaseButton-secondary"] *,
[data-testid^="stBaseLinkButton-secondary"],
[data-testid^="stBaseLinkButton-secondary"] *,
[data-testid="stLinkButton"] a[kind="secondary"],
[data-testid="stLinkButton"] a[kind="secondary"] *{
  color:#4C1D95 !important;
}

/* ---- inputs ---- */
[data-testid="stTextInputRootElement"], [data-testid="stNumberInputContainer"]{
  border-radius:10px !important; border:1px solid #E4DEF7 !important;
  background:#FBFAFF !important;
}
[data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stNumberInputContainer"]:focus-within{
  border-color:#6D28D9 !important; box-shadow:0 0 0 3px rgba(109,40,217,.12) !important;
}
[data-testid="stNumberInputContainer"] input{font-variant-numeric:tabular-nums;}
/* The steppers stay (they are useful on a phone) but stop competing with the
   figure itself. */
[data-testid="stNumberInputStepUp"], [data-testid="stNumberInputStepDown"]{
  background:transparent !important; color:#8578A6 !important; border:none !important;
}
[data-testid="stNumberInputStepUp"]:hover, [data-testid="stNumberInputStepDown"]:hover{
  color:#6D28D9 !important; background:#F2EEFD !important;
}
.stTextInput label, .stNumberInput label, .stSelectbox label{
  font-size:.85rem !important; font-weight:500 !important; color:#4A3D6B !important;
}
[data-baseweb="select"] > div{
  border-radius:10px !important; border-color:#E4DEF7 !important;
  background:#FBFAFF !important;
}
[data-baseweb="select"] > div:focus-within{
  border-color:#6D28D9 !important; box-shadow:0 0 0 3px rgba(109,40,217,.12) !important;
}

/* ---- expander + alerts ---- */
[data-testid="stExpander"]{border:1px solid #E4DEF7 !important;border-radius:12px !important;
  background:#FFFFFF !important;}
[data-testid="stExpander"] summary{font-size:.88rem !important;color:#4A3D6B !important;}
[data-testid="stAlert"]{border-radius:11px !important;}
</style>
"""

GATE_CSS = """
<style>
[data-testid="stMainBlockContainer"], .block-container {
  max-width:640px !important; padding-top:2.2rem !important; padding-bottom:3rem !important;
}

/* ---- brand header ----
   The hero carries its OWN background and text colours rather than inheriting
   the page's, so it survives a visitor whose Streamlit is in dark mode. */
.nyzhero{text-align:center;margin-bottom:26px;background:#F6F3FE;
  border:1px solid #E4DEF7;border-radius:20px;padding:26px 20px 24px;}
.nyzmark{width:62px;height:62px;margin:0 auto 16px;border-radius:18px;
  background:#FFFFFF;border:1.5px solid #6D28D9;display:flex;align-items:center;
  justify-content:center;}
.nyzname{font-family:'Fraunces',Georgia,serif;font-weight:700;font-size:1.85rem;
  color:#3B0F8F;letter-spacing:-.02em;line-height:1.15;margin-bottom:6px;}
.stApp .nyzsub{color:#4A3D6B;font-size:.98rem;max-width:42ch;margin:0 auto;}

/* ---- price band ---- */
.nyzprice{background:linear-gradient(135deg,#6D28D9,#8B5CF6);border-radius:18px;
  padding:24px 22px;text-align:center;color:#fff;margin:22px 0 10px;
  box-shadow:0 16px 40px -18px rgba(109,40,217,.65);}
.nyzamt{font-family:'Fraunces',serif;font-weight:700;font-size:2.9rem;line-height:1;
  font-variant-numeric:tabular-nums;}
.nyzamt small{font-size:.95rem;font-weight:400;opacity:.9;margin-left:7px;
  font-family:'DM Sans',sans-serif;}
.nyzterms{font-size:.88rem;opacity:.94;margin-top:7px;}

/* ---- trust row ---- */
.nyztrust{display:flex;gap:9px;margin:14px 0 18px;flex-wrap:wrap;}
.nyztrust div{flex:1 1 30%;background:#F6F3FE;border:1px solid #E4DEF7;
  border-radius:11px;padding:11px 8px;text-align:center;}
.nyztrust b{display:block;font-size:1.02rem;color:#6D28D9;font-weight:700;line-height:1.3;}
.nyztrust span{font-size:.76rem;color:#6B5E8A;}

/* ---- divider ---- */
.nyzsep{display:flex;align-items:center;gap:13px;margin:30px 0 16px;
  color:#6B5E8A;font-size:.86rem;font-weight:600;}
.nyzsep:before,.nyzsep:after{content:"";flex:1;height:1px;background:#E4DEF7;}

/* ---- form card ---- */
[data-testid="stForm"]{
  background:#FFFFFF !important; border:1px solid #E4DEF7 !important;
  border-radius:16px !important; padding:20px !important;
  box-shadow:0 12px 34px -20px rgba(76,29,149,.3) !important;
}

/* ---- notes ---- */
.stApp .nyzfine{font-size:.8rem;color:#6B5E8A;text-align:center;margin-top:16px;line-height:1.65;}
.stApp .nyzlegal{font-size:.73rem;color:#7A6D99;text-align:center;margin-top:22px;
  padding-top:16px;border-top:1px solid #EFEBFB;line-height:1.6;}

@media (max-width:640px){
  .nyzamt{font-size:2.4rem;}
  /* Keep the three stats on one row on a phone. Stacking them pushed the
     Buy button below the fold. min-width:0 lets them shrink instead. */
  .nyztrust{gap:6px;}
  .nyztrust div{flex:1 1 0;min-width:0;padding:9px 4px;}
  .nyztrust b{font-size:.94rem;}
  .nyztrust span{font-size:.64rem;}
}
</style>
"""


def theme(mode="app"):
    """Inject the shared look, plus the gate layer when asked for it."""
    st.markdown(BASE_CSS, unsafe_allow_html=True)
    if mode == "gate":
        st.markdown(GATE_CSS, unsafe_allow_html=True)


LOGO_SVG = """<svg viewBox="0 0 48 48" fill="none" width="32" height="32">
  <path d="M13 34V14l12 13.5V14" stroke="#6D28D9" stroke-width="3.8"
        stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="30" y="20" width="4" height="14" rx="1.6" fill="#8B5CF6"/>
  <rect x="36.5" y="25" width="4" height="9" rx="1.6" fill="#C4B5FD"/>
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


def sidebar(data, cfg=None):
    """Account box plus the licence watermark."""
    if not data:
        return
    cfg = cfg or config()
    with st.sidebar:
        st.markdown("### Account")
        st.caption(data.get("email", ""))
        exp = data.get("expires_at")
        if exp:
            left = int((exp - time.time()) / 86400)
            st.caption(f"Access until {datetime.fromtimestamp(exp, IST):%d %b %Y} "
                       f"({left} days left)")
        if data.get("comp"):
            st.caption("Free pass")
        if st.button("Sign out", use_container_width=True):
            if not data.get("comp"):
                device_forget(cfg, data.get("ref"),
                              st.session_state.get("nyz_device"))
            st.session_state.pop("nyz_token", None)
            st.session_state["nyz_do_clear"] = True
            st.rerun()

    st.markdown(
        # No negative margin: it used to pull this line into the container's
        # top padding, where it was clipped mid-glyph.
        f"<div style='text-align:right;font-size:.74rem;color:#6B5E8A;"
        f"margin:0 0 2px'>Licensed to {data.get('email','')}</div>",
        unsafe_allow_html=True)
