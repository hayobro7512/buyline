"""Render the approved PO in state.json as a PDF (headless Chrome, own throwaway profile). make(state) -> path or None."""
import html, json, pathlib, shutil, subprocess, sys, tempfile
from datetime import date

HERE = pathlib.Path(__file__).parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def page(st: dict) -> str:
    po, spec = st["po"], st["spec"]
    sup = next(s for s in st["suppliers"] if s["id"] == po["supplier"])
    q = next(q for q in st["quotes"] if q["id"] == po["supplier"])
    e = lambda x: html.escape(str(x))
    unit = (spec.get("unit") or "unit").rstrip("s")
    qty = max(spec.get("quantity") or 0, q.get("min_order") or 0)
    extras = (q.get("shipping") or 0) + (q.get("fees") or 0) + (q.get("duty_amt") or 0)
    saved = ((q.get("first_price") or q["unit_price"]) - q["unit_price"]) * qty
    rows = [(f"{spec.get('what') or spec.get('item')}", f"{qty} {spec.get('unit', '')}", f"${q['unit_price']:.2f} / {unit}",
             f"${q['unit_price'] * qty:.2f}")]
    if extras:
        rows.append(("Shipping, fees and duty", "", "", f"${extras:.2f}"))
    return f"""<!doctype html><meta charset="utf-8"><style>
body{{font:13px/1.5 -apple-system,Helvetica,sans-serif;color:#1d2430;margin:48px}}
h1{{font-size:26px;margin:0}} .muted{{color:#6b7280}} table{{width:100%;border-collapse:collapse;margin:24px 0}}
th,td{{text-align:left;padding:8px;border-bottom:1px solid #e5e7eb}} td:last-child,th:last-child{{text-align:right}}
.total{{font-size:18px;font-weight:700}} .grid{{display:flex;gap:48px;margin-top:24px}} .stamp{{color:#0f766e;font-weight:700}}
</style>
<div style="display:flex;justify-content:space-between"><div><h1>Buyline</h1><div class="muted">Purchase order</div></div>
<div style="text-align:right"><b>{e(po.get('number') or '')}</b><br>{date.today():%B %d, %Y}</div></div>
<div class="grid"><div><div class="muted">Supplier</div><b>{e(sup['name'])}</b></div>
<div><div class="muted">Ship to</div><b>{e(spec.get('ship_to') or '—')}</b></div>
<div><div class="muted">Delivery</div><b>within {e(q.get('lead_days') or '?')} days</b></div></div>
<table><tr><th>Item</th><th>Quantity</th><th>Unit price</th><th>Amount</th></tr>
{''.join(f'<tr><td>{e(a)}</td><td>{e(b)}</td><td>{e(c)}</td><td>{e(d)}</td></tr>' for a, b, c, d in rows)}
<tr><td colspan="3" class="total">Total</td><td class="total">${q['total']:.2f}</td></tr></table>
{f'<p class="stamp">Negotiated by Fo: ${saved:.2f} below the first quote.</p>' if saved > 0 else ''}
<p class="muted">Approved by the buyer in Buyline. Sent to the supplier by Fo.</p>"""


def make(st: dict) -> pathlib.Path | None:
    if not st.get("po") or not st["po"].get("supplier"):
        return None
    out = HERE / "po" / f"{st['po'].get('number') or 'PO'}.pdf"
    out.parent.mkdir(exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        (tmp / "po.html").write_text(page(st))
        subprocess.run([CHROME, "--headless", "--disable-gpu", f"--user-data-dir={tmp / 'profile'}", "--no-pdf-header-footer",
                        f"--print-to-pdf={out}", (tmp / "po.html").as_uri()], capture_output=True, timeout=60)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out if out.exists() else None


if __name__ == "__main__":
    print(make(json.loads((HERE / "state.json").read_text())) or "no PO in state.json")
