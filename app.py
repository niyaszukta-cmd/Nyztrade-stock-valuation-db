"""
NYZTrade Valuation Desk — paid Streamlit app.

Same five methods and the same arithmetic as the free HTML calculator, but the
code runs on the server, so the gate in nyz_access.py is enforced before any of
it reaches the browser. That is the whole reason this version exists: a single
HTML file cannot be sold, because the buyer already has the code.

The presentation layer below is deliberate. A valuation is only as trustworthy
as the working behind it, so the calculation trace and a price-against-value
scale carry the screen, rather than a single large number on a coloured block.
"""
import math
import re

import streamlit as st

from nyz_access import require_access, sidebar, config, theme, LOGO_SVG

st.set_page_config(page_title="NYZTrade Valuation Desk", page_icon="📊",
                   layout="wide", initial_sidebar_state="collapsed")

# --- the gate. Nothing below runs until the buyer is through. ---------------
BUYER = require_access()
CFG = config()
sidebar(BUYER, CFG)

theme("app")

st.markdown("""
<style>
[data-testid="stMainBlockContainer"], .block-container{
  max-width:1180px !important; padding-top:1.6rem !important; padding-bottom:3rem !important;
}

/* ---- masthead ---- */
/* The rule is a separate element, not a border on .nyzbar: the masthead now
   shares a column row with the theme toggle, so a border here would stop
   short of the page width. */
.nyzbar{display:flex;align-items:center;gap:13px;}
.nyzrule{height:1px;background:var(--bd);margin:10px 0 22px;}
.nyzbar .mk{width:42px;height:42px;border-radius:13px;background:var(--s3);
  border:1.5px solid var(--brand);color:var(--logo);display:flex;align-items:center;justify-content:center;
  flex:0 0 auto;}
.nyzbar .mk svg{width:24px;height:24px;}
.nyzbar h1{font-family:'Fraunces',Georgia,serif;font-weight:700;font-size:1.72rem;
  color:var(--ink);margin:0;line-height:1.1;letter-spacing:-.02em;}
.stApp .nyzbar p{margin:2px 0 0;font-size:.97rem;color:var(--ink2);max-width:62ch;}

/* ---- numbered steps. The form genuinely is a sequence, so the numbers
   carry information rather than decoration. ---- */
.nyzstep{display:flex;align-items:center;gap:10px;margin:26px 0 12px;}
.nyzstep:first-of-type{margin-top:4px;}
.nyzstep i{flex:0 0 auto;width:25px;height:25px;border-radius:8px;background:var(--brand);
  color:var(--onbrand);font-style:normal;font-weight:700;font-size:.9rem;display:flex;
  align-items:center;justify-content:center;}
.nyzstep h2{margin:0;font-size:1.14rem;font-weight:700;color:var(--ink);letter-spacing:-.01em;}

/* ---- benchmark pills ---- */
.nyzpills{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0 2px;}
.nyzpills span{background:var(--s3);border:1px solid var(--bd);border-radius:999px;
  padding:6px 12px;font-size:.88rem;color:var(--ink2);font-variant-numeric:tabular-nums;}
.nyzpills span b{color:var(--brandink);font-weight:700;}
.stApp .nyzbenchlead{font-size:.93rem;color:var(--ink2);margin:4px 0 0;}

/* ---- quiet note. Replaces Streamlit's blue st.info, which fought the
   purple everywhere else on the page. ---- */
.nyznote{border-left:3px solid var(--brand2);background:var(--s2);border-radius:0 10px 10px 0;
  padding:12px 15px;font-size:.95rem;color:var(--ink2);line-height:1.6;margin:10px 0 2px;}
.nyznote b{color:var(--brandink);}

/* ---- result panel. The one place with elevation, so the eye lands here. ---- */
.nyzres{border:1px solid var(--bd);border-radius:20px;padding:22px 22px 24px;
  background:var(--s1);box-shadow:0 20px 50px -34px var(--shadow);}
.nyzres .cap{font-size:.95rem;color:var(--ink3);margin-bottom:2px;}
.nyzfv{font-family:'Fraunces',Georgia,serif;font-weight:700;font-size:3.35rem;
  line-height:1.02;letter-spacing:-.025em;color:var(--ink);font-variant-numeric:tabular-nums;}
.nyzverdict{font-size:1.1rem;font-weight:700;margin-top:3px;}
.g-gain{color:var(--gain);} .g-warn{color:var(--brand2);} .g-loss{color:var(--loss);}

/* ---- price-against-value scale. This is the real finding: not "₹306" on its
   own but where ₹306 sits relative to what the market charges today. ---- */
.nyzscale{margin:20px 0 4px;}
.nyztrack{position:relative;height:10px;border-radius:999px;background:var(--s3);}
.nyzspan{position:absolute;top:0;height:10px;border-radius:999px;}
.nyzpin{position:absolute;top:50%;width:3px;height:22px;border-radius:2px;
  transform:translate(-50%,-50%);}
.nyzaxis{display:flex;justify-content:space-between;font-size:.8rem;color:var(--ink3);
  margin-top:7px;font-variant-numeric:tabular-nums;}
.nyzkey{display:flex;gap:18px;margin-top:11px;flex-wrap:wrap;}
.nyzkey div{font-size:.9rem;color:var(--ink2);display:flex;align-items:center;gap:7px;
  font-variant-numeric:tabular-nums;}
.nyzkey i{width:9px;height:9px;border-radius:3px;flex:0 0 auto;}
.nyzkey b{color:var(--ink);}

/* ---- stat tiles ---- */
.nyztiles{display:flex;gap:9px;margin-top:18px;}
/* Column layout with the figure pushed to the bottom, so a label that wraps
   to two lines does not leave its number sitting lower than its neighbours'. */
.nyztiles div{flex:1 1 0;min-width:0;background:var(--s2);border:1px solid var(--bd);
  border-radius:12px;padding:11px 12px;display:flex;flex-direction:column;
  justify-content:space-between;}
.nyztiles span{display:block;font-size:.85rem;color:var(--ink3);margin-bottom:3px;}
.nyztiles b{font-size:1.2rem;color:var(--ink);font-weight:700;
  font-variant-numeric:tabular-nums;}

/* ---- the working, set as a ledger rather than a grey code blob ---- */
.nyzwork{border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-top:8px;}
.nyzwork div{display:flex;justify-content:space-between;gap:14px;align-items:baseline;
  padding:11px 15px;font-size:.95rem;border-bottom:1px solid var(--bd);}
.nyzwork div:last-child{border-bottom:none;background:var(--s2);}
.nyzwork div:last-child s{font-weight:700;color:var(--ink);}
.nyzwork div:last-child u{font-weight:700;color:var(--brandink);}
.nyzwork s{text-decoration:none;color:var(--ink2);}
.nyzwork u{text-decoration:none;color:var(--ink);font-weight:600;white-space:nowrap;
  font-variant-numeric:tabular-nums;}

/* ---- tables ---- */
table.nyztbl{width:100%;border-collapse:collapse;font-size:.94rem;margin-top:8px;}
table.nyztbl th{text-align:left;font-weight:600;color:var(--ink3);font-size:.86rem;
  padding:9px 10px;border-bottom:1px solid var(--bd);}
table.nyztbl td{padding:10px 10px;border-bottom:1px solid var(--bd);color:var(--ink);}
table.nyztbl tr:last-child td{border-bottom:none;}
table.nyztbl .r{text-align:right;}
table.nyztbl .muted{color:var(--ink3);}
/* The "how it works" column is reference, not the answer. Capping it keeps
   each row one or two lines instead of letting the formula set row height. */
table.nyztbl td:nth-child(2){font-size:.87rem;line-height:1.45;}
table.nyztbl th:first-child, table.nyztbl td:first-child{white-space:nowrap;}

/* ---- section label inside the result ---- */
.nyzlab{font-size:1.02rem;font-weight:700;color:var(--ink);margin:22px 0 2px;}

/* ---- empty state: an invitation, not a shrug ---- */
.nyzempty{border:1px dashed var(--bd2);border-radius:18px;padding:40px 24px;
  text-align:center;background:var(--s2);}
.nyzempty b{display:block;font-size:1.08rem;color:var(--ink);margin-bottom:5px;}
.nyzempty span{font-size:.96rem;color:var(--ink3);}

.stApp .nyzdisc{font-size:.84rem;color:var(--ink3);line-height:1.65;margin-top:34px;
  padding-top:16px;border-top:1px solid var(--bd);}

@media (max-width:760px){
  .nyzfv{font-size:2.7rem;}
  .nyztiles{flex-wrap:wrap;}
  .nyztiles div{flex:1 1 30%;}
}
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# benchmarks
# ---------------------------------------------------------------------------
BENCH = {
    "Agricultural Chemicals": (18, 2.6, 11), "Air Delivery & Freight": (20, 2.8, 12),
    "Auto Manufacturers": (18, 2.2, 11), "Auto Parts": (20, 2.6, 11),
    "Banks — Private": (18, 2.6, 11), "Banks — PSU": (9, 1.1, 8),
    "Beverages": (38, 7.0, 24), "Cement": (22, 3.0, 13),
    "Chemicals — Specialty": (26, 4.0, 16), "Construction & Infra": (18, 2.2, 11),
    "Consumer Durables": (34, 6.0, 22), "Credit Services / NBFC": (16, 2.8, 10),
    "Electric Utilities": (14, 1.8, 9), "FMCG / Packaged Foods": (36, 7.5, 24),
    "Gas Utilities": (16, 2.4, 10), "Hotels & Leisure": (30, 4.0, 18),
    "Information Technology": (24, 4.2, 16), "Insurance": (20, 2.0, 12),
    "Metals — Non-ferrous": (13, 1.6, 8), "Oil & Gas": (10, 1.4, 7),
    "Paints": (45, 9.0, 30), "Pharmaceuticals": (28, 4.2, 17),
    "Power Generation": (14, 1.8, 9), "Real Estate": (20, 2.0, 13),
    "Retail": (40, 7.0, 25), "Steel & Iron": (12, 1.3, 7),
    "Telecom": (22, 3.0, 9), "Textiles": (14, 1.6, 9),
    "General / not listed": (20, 2.5, 12),
}
CAPM = {"Large cap": (1.0, 1.0, 1.0), "Mid cap": (1.15, 1.10, 1.10),
        "Small cap": (1.25, 1.20, 1.15)}

METHODS = {
    "pe":  ("Your P/E", 30, "EPS x your target P/E", "an EPS and a target P/E"),
    "ipe": ("Industry P/E", 25, "EPS x the peer multiple", "an EPS and an industry P/E"),
    "pb":  ("Price / Book", 20, "Book value x target P/B", "a book value"),
    "ev":  ("EV / EBITDA", 17, "(EBITDA x multiple - net debt) / shares",
            "EBITDA and a share count"),
    "gr":  ("Graham Number", 8, "sqrt(22.5 x EPS x BVPS)", "an EPS and a book value"),
}

NOTE = {
    "all": "All five, blended. The spread between them tells you more than the blend.",
    "pe": "**Best for** steady, profitable companies. **Breaks down** on negative, "
          "one-off or cyclical earnings — a cyclical at peak earnings is the classic trap.",
    "ipe": "**Best for** asking what the market already pays for comparable earnings. "
           "**Breaks down** when the whole sector is mispriced.",
    "pb": "**Best for** banks, NBFCs and asset-heavy businesses. **Nearly useless** for "
          "asset-light ones, which carry their value off the balance sheet.",
    "ev": "**Best for** comparing different debt loads. **Ignores** capex and interest, "
          "so it flatters businesses that must keep spending to stand still.",
    "gr": "**Best for** a sanity check on defensive companies. **Expect it to look low** "
          "— it caps P/E at 15 and P/B at 1.5. That is the point of a floor.",
}


def pos(x):
    try:
        return x is not None and float(x) > 0 and math.isfinite(float(x))
    except Exception:
        return False


def m_pe(eps, mult):
    return eps * mult if (pos(eps) and pos(mult)) else None


def m_pb(bvps, mult):
    return bvps * mult if (pos(bvps) and pos(mult)) else None


def m_ev(ebitda, mult, net_debt, shares):
    if not (pos(ebitda) and pos(mult) and pos(shares)):
        return None
    equity = ebitda * mult - (net_debt or 0)
    return equity / shares if equity > 0 else None


def m_graham(eps, bvps):
    return math.sqrt(22.5 * eps * bvps) if (pos(eps) and pos(bvps)) else None


def verdict(up):
    if up is None:
        return "hold", "Not enough data"
    if up > 25:
        return "strong", "Significantly undervalued"
    if up > 15:
        return "buy", "Undervalued"
    if up > 0:
        return "buy", "Fairly valued"
    if up > -10:
        return "hold", "Slightly expensive"
    return "avoid", "Overvalued"


def rs(x):
    return "N/A" if x is None else f"₹{x:,.2f}"


# verdict class -> (text colour class, marker colour)
TONE = {"strong": ("g-gain", "var(--gain)"), "buy": ("g-gain", "var(--gain)"),
        "hold": ("g-warn", "var(--brand2)"), "avoid": ("g-loss", "var(--loss)")}


def md_b(s):
    """The notes are authored with **bold**; render that inside our own HTML."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def scale_html(price, fv, marker):
    """
    A single axis from zero to a little past the higher of the two figures.
    Anchoring at zero keeps the gap honest: a bar that starts at the lower
    value would make a 5% difference look like a chasm.
    """
    hi = max(price, fv) * 1.12
    if hi <= 0:
        return ""
    p_pct, f_pct = price / hi * 100, fv / hi * 100
    lo_pct, span = min(p_pct, f_pct), abs(f_pct - p_pct)
    fill = "var(--gainfill)" if fv >= price else "var(--lossfill)"
    return (
        f'<div class="nyzscale"><div class="nyztrack">'
        f'<div class="nyzspan" style="left:{lo_pct:.2f}%;width:{span:.2f}%;'
        f'background:{fill}"></div>'
        f'<div class="nyzpin" style="left:{p_pct:.2f}%;background:var(--ink)"></div>'
        f'<div class="nyzpin" style="left:{f_pct:.2f}%;background:{marker}"></div>'
        f'</div><div class="nyzaxis"><span>₹0</span><span>{rs(hi)}</span></div>'
        f'<div class="nyzkey">'
        f'<div><i style="background:var(--ink)"></i>Market price <b>{rs(price)}</b></div>'
        f'<div><i style="background:{marker}"></i>Fair value <b>{rs(fv)}</b></div>'
        f'</div></div>'
    )


def table(headers, rows, right=()):
    """rows: list of lists. `right` holds the indexes to right-align."""
    th = "".join(f'<th class="{"r" if i in right else ""}">{h}</th>'
                 for i, h in enumerate(headers))
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            f'<td class="{"r" if i in right else ""}">{c}</td>'
            for i, c in enumerate(r)) + "</tr>"
    return f'<table class="nyztbl"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
_head, _ctrl = st.columns([5, 1], vertical_alignment="center")
with _head:
    st.markdown(
        f'<div class="nyzbar"><div class="mk">{LOGO_SVG}</div><div>'
        f'<h1>Valuation Desk</h1>'
        f'<p>Value a stock from figures you supply. Nothing is fetched, so the '
        f'numbers are yours and so is the judgement behind the multiples.</p>'
        f'</div></div>', unsafe_allow_html=True)
with _ctrl:
    # Reads back into theme() on the next run. Streamlit commits widget state
    # to session_state before it reruns, so the repaint is immediate, not a
    # render behind.
    st.toggle("Dark mode", key="nyz_dark")
st.markdown('<div class="nyzrule"></div>', unsafe_allow_html=True)

left, right = st.columns([1, 1], gap="large")

with left:
    st.markdown('<div class="nyzstep"><i>1</i><h2>The company</h2></div>',
                unsafe_allow_html=True)
    name = st.text_input("Company", placeholder="e.g. Infosys")
    c1, c2 = st.columns(2)
    industry = c1.selectbox("Industry", sorted(BENCH),
                            index=sorted(BENCH).index("Information Technology"))
    cap = c2.selectbox("Size", list(CAPM))

    b_pe, b_pb, b_ev = BENCH[industry]
    m1, m2, m3 = CAPM[cap]
    d_pe, d_pb, d_ev = round(b_pe * m1, 1), round(b_pb * m2, 2), round(b_ev * m3, 1)
    st.markdown(
        f'<p class="nyzbenchlead">A {cap.lower()} business in {industry} '
        f'typically trades around:</p>'
        f'<div class="nyzpills"><span>P/E <b>{d_pe}x</b></span>'
        f'<span>P/B <b>{d_pb}x</b></span>'
        f'<span>EV/EBITDA <b>{d_ev}x</b></span></div>'
        f'<p class="nyzbenchlead" style="color:#7A6D99;font-size:.79rem">'
        f'Starting points, not truth. Change them below if you disagree.</p>',
        unsafe_allow_html=True)

    st.markdown('<div class="nyzstep"><i>2</i><h2>Choose a method</h2></div>',
                unsafe_allow_html=True)
    mode = st.selectbox(
        "Valuation method",
        ["all", "pe", "ipe", "pb", "ev", "gr"],
        format_func=lambda k: ("All five, blended" if k == "all"
                               else METHODS[k][0]))
    st.markdown(f'<div class="nyznote">{md_b(NOTE[mode])}</div>',
                unsafe_allow_html=True)

    st.markdown('<div class="nyzstep"><i>3</i><h2>The numbers</h2></div>',
                unsafe_allow_html=True)
    need = {"all": set("pe ipe pb ev gr".split())}.get(mode, {mode})

    def show(key_set):
        return bool(key_set & need)

    price = st.number_input("Market price ₹/share", min_value=0.0, step=0.01,
                            format="%.2f")
    eps = bvps = shares = ebitda = ndebt = 0.0
    t_pe = t_ipe = t_pb = t_ev = 0.0

    if show({"pe", "ipe", "gr"}):
        eps = st.number_input("EPS ₹/share (TTM)", min_value=0.0, step=0.01,
                              format="%.2f")
    if show({"pb", "gr"}):
        bvps = st.number_input("Book value ₹/share", min_value=0.0, step=0.01,
                               format="%.2f")
    if show({"ev"}):
        cc1, cc2 = st.columns(2)
        shares = cc1.number_input("Shares (crore)", min_value=0.0, step=0.01,
                                  format="%.2f")
        ebitda = cc2.number_input("EBITDA (₹ crore)", min_value=0.0, step=1.0,
                                  format="%.1f")
        ndebt = st.number_input("Net debt (₹ crore, negative if net cash)",
                                step=1.0, format="%.1f")

    st.markdown('<div class="nyzstep"><i>4</i><h2>Multiples and safety margin</h2></div>',
                unsafe_allow_html=True)
    if show({"pe"}):
        t_pe = st.number_input("Your target P/E", min_value=0.0, value=float(d_pe), step=0.1)
    if show({"ipe"}):
        t_ipe = st.number_input("Industry P/E", min_value=0.0, value=float(d_pe), step=0.1)
    if show({"pb"}):
        t_pb = st.number_input("Target P/B", min_value=0.0, value=float(d_pb), step=0.05)
    if show({"ev"}):
        t_ev = st.number_input("Target EV/EBITDA", min_value=0.0, value=float(d_ev), step=0.1)
    mos = st.number_input("Margin of safety %", min_value=0.0, max_value=60.0,
                          value=15.0, step=5.0)

    go = st.button("Value this stock", type="primary", use_container_width=True)

with right:
    if not go:
        st.markdown(
            '<div class="nyzempty"><b>Your valuation appears here</b>'
            '<span>Fill in the figures on the left, then press '
            'Value this stock.</span></div>', unsafe_allow_html=True)
    elif not pos(price):
        st.markdown(
            '<div class="nyzempty"><b>Add a market price</b>'
            '<span>The fair value needs something to be compared against.</span>'
            '</div>', unsafe_allow_html=True)
    else:
        res = {
            "pe": m_pe(eps, t_pe), "ipe": m_pe(eps, t_ipe),
            "pb": m_pb(bvps, t_pb), "ev": m_ev(ebitda, t_ev, ndebt, shares),
            "gr": m_graham(eps, bvps),
        }
        if mode != "all":
            res = {k: (v if k == mode else None) for k, v in res.items()}

        live = {k: v for k, v in res.items() if pos(v)}
        if not live:
            st.markdown(
                '<div class="nyzempty"><b>Not enough to work with</b><span>'
                + (f"This method needs {METHODS[mode][3]}." if mode != "all"
                   else "No method could run. Try an EPS with a target P/E.")
                + '</span></div>', unsafe_allow_html=True)
        else:
            total_w = sum(METHODS[k][1] for k in live)
            fv = sum(v * METHODS[k][1] / total_w for k, v in live.items())
            fv *= (1 - mos / 100)
            up = (fv - price) / price * 100
            cls, label = verdict(up)
            tone, marker = TONE[cls]

            pe_now = f"{price/eps:.1f}x" if pos(eps) else "—"
            who = f"{name.strip()} — " if name.strip() else ""
            st.markdown(
                f'<div class="nyzres">'
                f'<div class="cap">{who}fair value, after a {mos:.0f}% margin of safety</div>'
                f'<div class="nyzfv">{rs(fv)}</div>'
                f'<div class="nyzverdict {tone}">{label}, {up:+.1f}% against '
                f'today’s price</div>'
                f'{scale_html(price, fv, marker)}'
                f'<div class="nyztiles">'
                f'<div><span>Methods used</span><b>{len(live)}</b></div>'
                f'<div><span>P/E you pay now</span>'
                f'<b>{pe_now}</b></div>'
                f'<div><span>Gap to price</span><b>{up:+.1f}%</b></div>'
                f'</div></div>', unsafe_allow_html=True)

            if mode != "all":
                raw = live[mode]
                steps = []
                if mode in ("pe", "ipe"):
                    mult = t_pe if mode == "pe" else t_ipe
                    steps.append((f"EPS {rs(eps)} × {mult:.1f}x", rs(raw)))
                elif mode == "pb":
                    steps.append((f"Book value {rs(bvps)} × {t_pb:.2f}x", rs(raw)))
                elif mode == "ev":
                    ev_val = ebitda * t_ev
                    steps.append((f"EBITDA ₹{ebitda:,.0f} Cr × {t_ev:.1f}x",
                                  f"₹{ev_val:,.0f} Cr"))
                    steps.append((f"{'Less' if ndebt >= 0 else 'Plus'} net "
                                  f"{'debt' if ndebt >= 0 else 'cash'} "
                                  f"₹{abs(ndebt):,.0f} Cr",
                                  f"₹{ev_val - ndebt:,.0f} Cr equity"))
                    steps.append((f"Divided by {shares:,.2f} Cr shares", rs(raw)))
                else:
                    steps.append((f"√(22.5 × {rs(eps)} × {rs(bvps)})",
                                  rs(raw)))
                steps.append((f"Less a {mos:.0f}% margin of safety", rs(fv)))

                st.markdown('<div class="nyzlab">The working</div>',
                            unsafe_allow_html=True)
                st.markdown(
                    '<div class="nyzwork">'
                    + "".join(f"<div><s>{a}</s><u>{b}</u></div>" for a, b in steps)
                    + "</div>", unsafe_allow_html=True)
            else:
                rows = []
                for k, (nm, w, how, needs) in METHODS.items():
                    v = res[k]
                    rows.append([
                        nm,
                        f'<span class="muted">{how}</span>',
                        rs(v) if pos(v) else f'<span class="muted">needs {needs}</span>',
                        f"{(v-price)/price*100:+.1f}%" if pos(v) else
                        '<span class="muted">—</span>',
                        f"{METHODS[k][1]/total_w*100:.0f}%" if k in live else
                        '<span class="muted">—</span>',
                    ])
                st.markdown('<div class="nyzlab">Method by method</div>',
                            unsafe_allow_html=True)
                st.markdown(table(["Method", "How it works", "Value", "Gap", "Weight"],
                                  rows, right=(2, 3, 4)), unsafe_allow_html=True)

                vals = sorted(live.values())
                lo, hi = vals[0], vals[-1]
                spread = (hi - lo) / lo * 100 if lo else 0
                st.markdown(
                    f'<div class="nyztiles" style="margin-top:14px">'
                    f'<div><span>Lowest</span><b>{rs(lo)}</b></div>'
                    f'<div><span>Highest</span><b>{rs(hi)}</b></div>'
                    f'<div><span>Spread</span><b>{spread:.0f}%</b></div>'
                    f'</div>', unsafe_allow_html=True)
                if spread > 120:
                    st.markdown(
                        '<div class="nyznote" style="border-left-color:var(--loss)">'
                        '<b>The methods disagree sharply.</b> A single blended number '
                        'across a range this wide is false precision. Find the outlier '
                        'and why before leaning on it.</div>', unsafe_allow_html=True)
                elif spread < 25:
                    st.markdown(
                        '<div class="nyznote" style="border-left-color:var(--gain)">'
                        '<b>The methods agree closely</b>, which is a stronger signal '
                        'than any one of them alone.</div>', unsafe_allow_html=True)

            if pos(eps) and pos(t_pe or t_ipe):
                mult = t_pe or t_ipe
                st.markdown('<div class="nyzlab">If you are wrong</div>',
                            unsafe_allow_html=True)
                srows = []
                for c, e, m in (("Bear", .8, .75), ("Base", 1, 1), ("Bull", 1.2, 1.25)):
                    val = eps * e * mult * m * (1 - mos / 100)
                    gap = (val - price) / price * 100
                    srows.append([c, rs(eps * e), f"{mult*m:.1f}x", rs(val),
                                  f'<span class="{"g-gain" if gap >= 0 else "g-loss"}">'
                                  f'{gap:+.1f}%</span>'])
                st.markdown(table(["Case", "EPS", "P/E", "Value", "Gap"],
                                  srows, right=(1, 2, 3, 4)), unsafe_allow_html=True)

st.markdown(
    '<div class="nyzdisc"><b>Educational tool.</b> This calculator does arithmetic on '
    'figures you type in. It does not verify them, fetch market data, or know anything '
    'about the company. Its output is not investment advice, not a recommendation and '
    'not a price target. NYZTrade is not a SEBI-registered investment adviser. Verify '
    'every figure against the company’s filings.</div>',
    unsafe_allow_html=True)
