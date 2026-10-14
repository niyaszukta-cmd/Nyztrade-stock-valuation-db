"""
NYZTrade Valuation Desk — paid Streamlit app.

Same five methods and the same arithmetic as the free HTML calculator, but the
code runs on the server, so the gate in nyztrade_access.py is enforced before
any of it reaches the browser. That is the whole reason this version exists: a
single HTML file cannot be sold, because the buyer already has the code.
"""
import math

import streamlit as st

from nyz_access import require_access, sidebar, config

st.set_page_config(page_title="NYZTrade Valuation Desk", page_icon="📊",
                   layout="wide", initial_sidebar_state="collapsed")

# --- the gate. Nothing below runs until the buyer is through. ---------------
BUYER = require_access()
CFG = config()
sidebar(BUYER, CFG)

st.markdown("""
<style>
  .big{font-size:2.4rem;font-weight:800;line-height:1.1;margin:.1rem 0 .3rem}
  .verdict{border-radius:14px;padding:22px;color:#fff;text-align:center;margin-bottom:6px}
  .v-strong{background:linear-gradient(135deg,#047857,#10B981)}
  .v-buy{background:linear-gradient(135deg,#0E7490,#06B6D4)}
  .v-hold{background:linear-gradient(135deg,#B45309,#F59E0B)}
  .v-avoid{background:linear-gradient(135deg,#B91C1C,#EF4444)}
  .vk{font-size:.78rem;letter-spacing:.09em;text-transform:uppercase;opacity:.9}
  .vs{font-size:.9rem;opacity:.93}
  .work{background:rgba(128,128,128,.09);border-radius:10px;padding:14px 16px;
        font-family:ui-monospace,monospace;font-size:.88rem;line-height:1.9}
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


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Valuation Desk")
st.caption("Value a stock from figures you supply. Nothing is fetched — the numbers "
           "are yours, and so is the judgement behind the multiples.")

left, right = st.columns(2, gap="large")

with left:
    st.subheader("1. The company")
    name = st.text_input("Company", placeholder="e.g. Infosys")
    c1, c2 = st.columns(2)
    industry = c1.selectbox("Industry", sorted(BENCH), index=sorted(BENCH).index("Information Technology"))
    cap = c2.selectbox("Size", list(CAPM))

    b_pe, b_pb, b_ev = BENCH[industry]
    m1, m2, m3 = CAPM[cap]
    d_pe, d_pb, d_ev = round(b_pe * m1, 1), round(b_pb * m2, 2), round(b_ev * m3, 1)
    st.info(f"**{industry}** · {cap} — benchmark P/E **{d_pe}x**, P/B **{d_pb}x**, "
            f"EV/EBITDA **{d_ev}x**. Starting points, not truth.")

    st.subheader("2. Choose a method")
    mode = st.selectbox(
        "Valuation method",
        ["all", "pe", "ipe", "pb", "ev", "gr"],
        format_func=lambda k: ("All five — blended" if k == "all"
                               else METHODS[k][0]))
    st.info(NOTE[mode])

    st.subheader("3. The numbers")
    need = {"all": set("pe ipe pb ev gr".split())}.get(mode, {mode})

    def show(key_set):
        return bool(key_set & need)

    price = st.number_input("Market price ₹/share", min_value=0.0, step=0.01, format="%.2f")
    eps = bvps = shares = ebitda = ndebt = 0.0
    t_pe = t_ipe = t_pb = t_ev = 0.0

    if show({"pe", "ipe", "gr"}):
        eps = st.number_input("EPS ₹/share (TTM)", min_value=0.0, step=0.01, format="%.2f")
    if show({"pb", "gr"}):
        bvps = st.number_input("Book value ₹/share", min_value=0.0, step=0.01, format="%.2f")
    if show({"ev"}):
        cc1, cc2 = st.columns(2)
        shares = cc1.number_input("Shares (crore)", min_value=0.0, step=0.01, format="%.2f")
        ebitda = cc2.number_input("EBITDA (₹ crore)", min_value=0.0, step=1.0, format="%.1f")
        ndebt = st.number_input("Net debt (₹ crore, negative if net cash)",
                                step=1.0, format="%.1f")

    st.subheader("4. Multiples and safety margin")
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
    st.subheader("Result")
    if not go:
        st.info("Fill in the fields on the left and press **Value this stock**.")
    elif not pos(price):
        st.warning("A market price is needed to compare against.")
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
            st.warning(f"This method needs {METHODS[mode][3]}."
                       if mode != "all" else
                       "No method could run. Try an EPS with a target P/E.")
        else:
            total_w = sum(METHODS[k][1] for k in live)
            fv = sum(v * METHODS[k][1] / total_w for k, v in live.items())
            fv *= (1 - mos / 100)
            up = (fv - price) / price * 100
            cls, label = verdict(up)

            st.markdown(
                f'<div class="verdict v-{cls}">'
                f'<div class="vk">Fair value · after {mos:.0f}% margin of safety</div>'
                f'<div class="big">{rs(fv)}</div>'
                f'<div><b>{label}</b></div>'
                f'<div class="vs">{up:+.1f}% vs market price of {rs(price)}</div></div>',
                unsafe_allow_html=True)

            k1, k2, k3 = st.columns(3)
            k1.metric("Methods used", len(live))
            k2.metric("P/E you pay now", f"{price/eps:.1f}x" if pos(eps) else "—")
            k3.metric("Gap to price", f"{up:+.1f}%")

            if mode != "all":
                raw = live[mode]
                steps = []
                if mode in ("pe", "ipe"):
                    mult = t_pe if mode == "pe" else t_ipe
                    steps.append(f"EPS {rs(eps)} × {mult:.1f}x = {rs(raw)}")
                elif mode == "pb":
                    steps.append(f"Book {rs(bvps)} × {t_pb:.2f}x = {rs(raw)}")
                elif mode == "ev":
                    ev_val = ebitda * t_ev
                    steps.append(f"EBITDA ₹{ebitda:,.0f} Cr × {t_ev:.1f}x = ₹{ev_val:,.0f} Cr")
                    steps.append(f"{'−' if ndebt>=0 else '+'} net "
                                 f"{'debt' if ndebt>=0 else 'cash'} ₹{abs(ndebt):,.0f} Cr "
                                 f"= ₹{ev_val-ndebt:,.0f} Cr equity")
                    steps.append(f"÷ {shares:,.2f} Cr shares = {rs(raw)}")
                else:
                    steps.append(f"√(22.5 × {rs(eps)} × {rs(bvps)}) = {rs(raw)}")
                steps.append(f"− margin of safety {mos:.0f}% = {rs(fv)}")
                st.markdown("**The working**")
                st.markdown('<div class="work">' + "<br>".join(steps) + "</div>",
                            unsafe_allow_html=True)
            else:
                rows = []
                for k, (nm, w, how, needs) in METHODS.items():
                    v = res[k]
                    rows.append({
                        "Method": nm, "How": how,
                        "Value": rs(v) if pos(v) else f"needs {needs}",
                        "Gap": f"{(v-price)/price*100:+.1f}%" if pos(v) else "—",
                        "Weight": f"{METHODS[k][1]/total_w*100:.0f}%" if k in live else "—",
                    })
                st.dataframe(rows, use_container_width=True, hide_index=True)

                vals = sorted(live.values())
                lo, hi = vals[0], vals[-1]
                s1, s2, s3 = st.columns(3)
                s1.metric("Lowest", rs(lo))
                s2.metric("Highest", rs(hi))
                spread = (hi - lo) / lo * 100 if lo else 0
                s3.metric("Spread", f"{spread:.0f}%")
                if spread > 120:
                    st.warning("**The methods disagree sharply.** A single blended number "
                               "across a range this wide is false precision — find the "
                               "outlier and why before leaning on it.")
                elif spread < 25:
                    st.info("**The methods agree closely**, which is a stronger signal "
                            "than any one of them alone.")

            if pos(eps) and pos(t_pe or t_ipe):
                mult = t_pe or t_ipe
                st.markdown("**If you are wrong**")
                st.dataframe([
                    {"Case": c, "EPS": rs(eps * e), "P/E": f"{mult*m:.1f}x",
                     "Value": rs(eps * e * mult * m * (1 - mos / 100)),
                     "Gap": f"{(eps*e*mult*m*(1-mos/100)-price)/price*100:+.1f}%"}
                    for c, e, m in (("Bear", .8, .75), ("Base", 1, 1), ("Bull", 1.2, 1.25))
                ], use_container_width=True, hide_index=True)

st.divider()
st.caption(
    "**Educational tool.** This calculator does arithmetic on figures you type in. It "
    "does not verify them, fetch market data, or know anything about the company. Its "
    "output is not investment advice, not a recommendation and not a price target. "
    "NYZTrade is not a SEBI-registered investment adviser. Verify every figure against "
    "the company's filings."
)
