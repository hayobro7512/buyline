#!/usr/bin/env python3
"""Buyline orchestrator: request text -> spec -> quotes from suppliers via Fo -> negotiate -> approve -> PO.
Every step is written to state.json; index.html polls it.
  python3 orchestrate.py "500 m of 12AWG copper wire, within two weeks, budget $500, ship to Berkeley CA"
  options: --fo stub|dry|live  --replies FILE (canned Fo replies, see REPLIES_EXAMPLE) --pause SECS --until STAGE
           --out FILE --no-llm --no-memory
  self-check: python3 orchestrate.py --check"""
import argparse, json, os, pathlib, re, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
memory = None  # imported lazily when memory is on
from datetime import datetime, date, timedelta

HERE = pathlib.Path(__file__).resolve().parent
STAGES = ["need", "suppliers", "calling", "quotes", "negotiate", "approve", "ordered"]
# ponytail: demo list of 3 suppliers from the plan; a --replies file can override it
SUPPLIERS = [
    {"id": "A", "name": "Supplier A", "phone": "+1 555 010 0001", "email": "orders@supplier-a.example"},
    {"id": "B", "name": "Supplier B", "phone": "+1 555 010 0002", "email": "orders@supplier-b.example"},
    {"id": "C", "name": "Supplier C", "phone": "+1 555 010 0003", "email": "orders@supplier-c.example"},
]
REPLIES_EXAMPLE = {  # what a --replies file looks like: Fo's summary of each call, keyed by supplier id
    "suppliers": SUPPLIERS,
    "quotes": {
        "A": "Spoke with Supplier A. Unit price is $0.95 per meter, minimum order 300 meters, lead time 10 days, shipping included.",
        "B": "Spoke with Supplier B. Unit price is $0.80 per meter, minimum order 1000 meters, lead time 5 days, shipping included.",
        "C": "Spoke with Supplier C. Unit price is $0.88 per meter, minimum order 200 meters, lead time 7 days, shipping included.",
    },
    "negotiate": {  # per supplier called back ("*" = anyone else); a plain string works too
        "C": "Called Supplier C back and said another supplier offered $0.80 per meter. They hesitated, then "
             "agreed to $0.78 per meter, minimum order 200 meters, lead time 7 days, shipping included.",
        "*": "They would not move from their quoted price. Terms unchanged.",
    },
    "status": "Called Supplier C's order desk. The order shipped this morning by ground freight, tracking 1Z999AA1, "
              "and should be delivered in 3 days.",
}
ORDERS = HERE / "orders.json"


from fo import make as make_fo, HANGUP, NO_ANSWER, PENDING, SEP  # stub | dry | live adapters
CALL_SECS = float(os.environ.get("BUYLINE_CALL_SECS", 6))  # pre-run replay: seconds per "dialing" and "on the phone"


# --- Haiku over plain urllib for the two extraction jobs; regex fallback when there is no key or --no-llm ---
def api_key() -> str:
    k = os.environ.get("ANTHROPIC_API_KEY")
    envf = HERE.parent / ".env"
    if not k and envf.exists():
        m = re.search(r"^ANTHROPIC_API_KEY=\"?([^\"\n]+)", envf.read_text(), re.M)
        k = m and m.group(1)
    return k or ""


USE_LLM = True


def llm_json(prompt: str, image: pathlib.Path | None = None) -> dict | None:
    key = api_key()
    if not key or not USE_LLM:
        return None
    content = [{"type": "text", "text": prompt}]
    if image:  # photo of an invoice, label or part
        import base64
        mt = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
        content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": mt,
                                                       "data": base64.b64encode(image.read_bytes()).decode()}})
    body = json.dumps({"model": os.environ.get("LLM_MODEL", "claude-haiku-5-5"), "max_tokens": 600,
                       "thinking": {"type": "disabled"}, "messages": [{"role": "user", "content": content}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", body, {
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        out = json.load(urllib.request.urlopen(req, timeout=30))
        text = next((b["text"] for b in out["content"] if b["type"] == "text"), "")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:  # fall back to regex, keep going
        print("llm:", repr(e)[:120], file=sys.stderr)
        return None


UNITS = {"m": "meters", "meter": "meters", "meters": "meters", "ft": "feet", "feet": "feet", "pcs": "pieces",
         "pieces": "pieces", "units": "units", "boxes": "boxes", "rolls": "rolls"}


QTY_RE = r"(\d[\d,]*)\s*(m|meters?|ft|feet|pcs|pieces|units|boxes|rolls)\b\s*(?:of\s+)?"


def parse_spec(text: str, intake: str = "", photo: pathlib.Path | None = None) -> dict:
    """One spec for the first item; every item of a multi-line list lands in spec['items'] and the rest of them,
    as text lines, in spec['queued_next'] for the brief. One sourcing loop at a time."""
    spec = _parse_spec(text, intake, photo)
    items = [i for i in (spec.pop("items", None) or []) if isinstance(i, dict) and i.get("name")]
    spec["items"] = items or [{"name": spec["item"], "qty": spec["quantity"], "unit": spec["unit"]}]
    spec["queued_next"] = [f"{i.get('qty') or ''} {i.get('unit') or ''} {i['name']}".strip() for i in spec["items"][1:]]
    return spec


def _parse_spec(text: str, intake: str = "", photo: pathlib.Path | None = None) -> dict:
    """text: the request. intake: pasted old invoice / spec / email. photo: image of an invoice or the item."""
    j = llm_json(
        "Extract a purchase request. The request may be short, and may come with an old invoice, a spec sheet or a "
        "photo: take item, size, features and (unless the request overrides them) quantity and ship-to from those. "
        "If several items are listed, item/quantity/unit describe the FIRST one and items lists them all, in order. "
        "Reply with JSON only:\n"
        '{"item":"<product, short>","size":"<size or gauge or dimensions, or null>","features":["<required feature>"],'
        '"quantity":<number>,"unit":"<meters|pieces|boxes|...>","lead_days":<max days to delivery, or null>,'
        '"budget":<total budget number, or null>,"target_price":<price per unit the user wants to pay, or null>,'
        '"ship_to":"<destination or null>","items":[{"name":"<item>","qty":<number>,"unit":"<unit>"}]}\n'
        f"Only what the text says, no guesses.\n\nRequest: {text}"
        + (f"\n\nAttached document:\n{intake[:6000]}" if intake else "") + ("\n\n(Photo attached.)" if photo else ""),
        photo)
    if j and j.get("quantity"):
        return {"item": j.get("item") or text, "size": j.get("size"), "features": j.get("features") or [],
                "quantity": int(j["quantity"]), "unit": j.get("unit") or "units", "lead_days": j.get("lead_days"),
                "budget": j.get("budget"), "target_price": j.get("target_price"), "ship_to": j.get("ship_to"), "raw": text,
                "items": j.get("items"),
                "source": "photo" if photo else "document" if intake else "text"}
    # regex fallback: quantity, unit, item, budget, lead time
    qty = re.search(QTY_RE, text, re.I)
    items = [{"name": re.split(r",|\bwithin\b|\bbudget\b|\$|\bship|\bat\b", m.group(3), 1, flags=re.I)[0].strip(" .-"),
              "qty": int(m.group(1).replace(",", "")), "unit": UNITS.get(m.group(2).lower(), m.group(2))}
             for m in re.finditer(QTY_RE + r"([^\n;]+)", text, re.I)]
    budget = re.search(r"budget\D{0,10}\$\s?(\d[\d,]*)|\$\s?(\d[\d,]*)\s*budget", text, re.I)
    tp = re.search(r"target(?: price)?\D{0,10}\$\s?(\d+(?:\.\d+)?)", text, re.I)
    lead = re.search(r"(?:within|in)\s+(\d+|a|one|two|three)\s+(day|week)s?", text, re.I)
    ship = re.search(r"ship(?:ping)?\s+to\s+([^,.]+)", text, re.I)
    words = {"a": 1, "one": 1, "two": 2, "three": 3}
    item = text[qty.end():] if qty else text
    item = re.split(r",|\n|\bwithin\b|\bin \d|\bbudget\b|\$|\bship", item, maxsplit=1, flags=re.I)[0].strip(" .") or text
    days = None
    if lead:
        k = lead.group(1).lower()
        days = (words.get(k) or int(k)) * (7 if lead.group(2).lower() == "week" else 1)
    return {"item": item, "size": None, "features": [], "quantity": int(qty.group(1).replace(",", "")) if qty else 1,
            "unit": UNITS.get(qty.group(2).lower(), qty.group(2)) if qty else "units", "lead_days": days,
            "budget": int((budget.group(1) or budget.group(2)).replace(",", "")) if budget else None,
            "target_price": float(tp.group(1)) if tp else None,
            "ship_to": ship.group(1).strip() if ship else None, "raw": text, "items": items}


def parse_quote(transcript: str, spec: dict) -> dict:
    j = llm_json(
        "This is Fo's summary of a phone call asking a supplier for a quote. Extract the final terms the supplier "
        "offered (if they moved during the call, the last price; if several reports are separated by ---, the last "
        "report is the outcome and earlier ones are history). Reply with JSON only:\n"
        '{"unit_price":<number or null>,"min_order":<minimum order quantity, 0 if none>,"lead_days":<days, null if '
        'unknown>,"shipping":<shipping cost number, 0 if included or free, null if unknown>,'
        '"fees":<other one-off fees in money (setup, handling, rush), 0 if none>,"duty":<import duty or tariff: a '
        'number if money, or a string like "8%" if a rate, 0 if none or domestic>,'
        '"price_breaks":[{"qty":<number>,"unit_price":<number>}],"conditions":["<rush fee, payment terms, any '
        'condition, short>"]}\n'
        f"We need {spec['quantity']} {spec['unit']}. Only what the summary says.\n\nSummary: {transcript}")
    if j and j.get("unit_price") is not None:
        return {"unit_price": float(j["unit_price"]), "min_order": int(j.get("min_order") or 0),
                "lead_days": j.get("lead_days"), "shipping": j.get("shipping"), "fees": j.get("fees") or 0,
                "duty": j.get("duty") or 0, "price_breaks": j.get("price_breaks") or [],
                "conditions": j.get("conditions") or []}
    transcript = transcript.split(SEP)[-1]  # several reports: the last is the outcome
    prices = re.findall(r"\$\s?(\d+(?:\.\d+)?)", transcript)  # last figure is what they agreed to
    moq = re.search(r"minimum order (?:of )?(\d[\d,]*)", transcript, re.I)
    lead = re.search(r"lead time (?:of )?(\d+)\s*(day|week)", transcript, re.I)
    days = int(lead.group(1)) * (7 if lead and lead.group(2).lower() == "week" else 1) if lead else None
    return {"unit_price": float(prices[-1]) if prices else None, "min_order": int(moq.group(1).replace(",", "")) if moq else 0,
            "lead_days": days, "shipping": 0 if re.search(r"shipping (included|free)", transcript, re.I) else None,
            "fees": 0, "duty": 0, "price_breaks": [], "conditions": []}


def total(q: dict, spec: dict) -> float | None:
    """Landed price: goods at the quantity you must buy (minimum order wins) + shipping + fees + duty."""
    if q["unit_price"] is None:
        return None
    n = max(spec["quantity"], q["min_order"])
    price = q["unit_price"]
    for b in sorted(q.get("price_breaks") or [], key=lambda b: b["qty"]):  # price at our quantity
        if n >= b["qty"]:
            price = b["unit_price"]
    goods = price * n
    duty = q.get("duty") or 0
    if isinstance(duty, str):  # "8%" of goods
        m = re.search(r"(\d+(?:\.\d+)?)", duty)
        duty = goods * float(m.group(1)) / 100 if m and "%" in duty else float(m.group(1)) if m else 0
    q["goods"], q["duty_amt"] = round(goods, 2), round(duty, 2)
    return round(goods + (q.get("shipping") or 0) + (q.get("fees") or 0) + duty, 2)


def recommend(quotes: list[dict], spec: dict, haggled: bool = False) -> dict:
    """Cheapest real cost that fits every constraint; name each trap so the reader sees why it lost.
    The budget is the hard line, checked once haggling is done: an over-budget quote can still be negotiated under."""
    notes, ok = [], []
    for q in quotes:
        if q["total"] is None:
            notes.append(f"{q['name']}: no usable quote")
        elif spec.get("lead_days") and q["lead_days"] and q["lead_days"] > spec["lead_days"]:
            notes.append(f"{q['name']}: lead time {q['lead_days']} days misses the {spec['lead_days']}-day deadline")
        elif q["min_order"] > spec["quantity"]:
            notes.append(f"{q['name']}: lowest-looking unit price ${q['unit_price']:.2f}, but minimum order "
                         f"{q['min_order']} > {spec['quantity']} needed, so you'd pay ${q['total']:.2f}")
        elif haggled and spec.get("budget") and q["total"] > spec["budget"]:
            notes.append(f"{q['name']}: ${q['total']:.2f} is over the ${spec['budget']} budget")
        else:
            ok.append(q)
    best = min(ok, key=lambda q: q["total"]) if ok else None
    if best:
        extras = (best.get("shipping") or 0) + (best.get("fees") or 0) + (best.get("duty_amt") or 0)
        notes.insert(0, f"Pick {best['name']}: landed ${best['total']:.2f} for {spec['quantity']} {spec['unit']} at "
                        f"${best['unit_price']:.2f}" + (f" plus ${extras:.2f} shipping, fees and duty" if extras else "")
                        + f", {best['lead_days']} days"
                        + (f"; conditions: {', '.join(best['conditions'])}" if best.get("conditions") else ""))
        for q in quotes:
            if q is not best and q["total"] is not None and q["total"] > best["total"]:
                notes.append(f"Catching {q['name']}'s trap saved ${q['total'] - best['total']:.2f}")
    return {"supplier": best["id"] if best else None, "why": notes}


def last_paid(mem: list[str]) -> tuple[float, str] | None:
    """(unit price, supplier) of the most recent order in memory lines like '... quoted $0.88 ...; negotiated down
    to $0.78. We ordered from Supplier C.'"""
    for m in mem:
        if "We ordered from" in m:
            prices = re.findall(r"\$(\d+(?:\.\d+)?)", m.split("minimum order")[0] + m[m.find("negotiated"):] if "negotiated" in m else m.split("minimum order")[0])
            who = re.search(r"We ordered from ([^.]+)\.", m)
            if prices:
                return float(prices[-1]), who.group(1) if who else "us"
    return None


# --- order tracker: one record per PO in orders.json; ordered -> shipped -> delivered, ETA and a late flag ---
def market_check(spec: dict, market=True) -> dict | None:
    """market.py's web-search range -> the need card's 'Market check' line. market may be a callable (tests),
    False (skip) or True (research). Soft is clamped to hard; a budget under the typical price is called out."""
    if not market:
        return None
    if callable(market):
        res = market(spec)
    else:
        import market as mk  # lazy: it imports this module
        res = mk.research(spec)
    res = dict(res or {})
    unit = spec["unit"].rstrip("s")
    if res.get("error") or not res.get("typical"):
        res["line"] = f"Market check unavailable ({res.get('error') or 'no range'}); rival quote and budget rules apply"
        return res
    if res.get("soft_line") and res.get("hard_line"):
        res["soft_line"] = min(res["soft_line"], res["hard_line"])
    res["line"] = f"Market check: ${res['low']:.2f}-${res['high']:.2f} per {unit}, typical ${res['typical']:.2f}"
    if spec.get("budget") and spec.get("quantity") and spec["budget"] / spec["quantity"] < res["typical"]:
        res["line"] += f" · budget is below market (${spec['budget'] / spec['quantity']:.2f} per {unit})"
    return res


def sample_replies(spec: dict) -> dict:
    """Sample mode (the public link): canned Fo call reports written for THIS item, no phone involved. Same story as
    the example file (A pricey, B cheapest but a minimum-order trap, C fits and haggles down), scaled to the request:
    the base price is the budget per unit when given, else $1.00, so the recommended deal lands under budget."""
    qty, unit = max(int(spec.get("quantity") or 1), 1), spec.get("unit") or "units"
    base = (spec["budget"] / qty) if spec.get("budget") else 1.0
    px = lambda f: f"${base * f:,.2f} per {unit.rstrip('s')}"
    moq = lambda f: f"minimum order {max(int(round(qty * f, -1)) or 1, 1):,} {unit}"
    return {"suppliers": SUPPLIERS, "quotes": {
        "A": f"Spoke with Supplier A. Unit price is {px(1.19)}, {moq(0.6)}, lead time 10 days, shipping included.",
        "B": f"Spoke with Supplier B. Unit price is {px(0.95)}, {moq(2.0)}, lead time 5 days, shipping included.",
        "C": f"Spoke with Supplier C. Unit price is {px(1.10)}, {moq(0.4)}, lead time 7 days, shipping included."},
        "negotiate": {"C": f"Called Supplier C back and said another supplier offered {px(0.95)}. They hesitated, then agreed to "
                           f"{px(0.975)}, {moq(0.4)}, lead time 7 days, shipping included.",
                      "*": "They would not move from their quoted price. Terms unchanged."}}


def quote_terms(x: dict, unit: str) -> str:
    return (f"minimum order {x['min_order']} {unit}, lead time {x['lead_days']} days"
            + (f", shipping ${x['shipping']:.2f}" if x.get("shipping") else ", shipping included"))


def haggle(their: float, target: float, max_unit: float | None = None) -> dict:
    """Code picks every number; Fo only says them. target = our best lever (rival quote or last paid).
    open ~90% of target; raises close half the gap and shrink; best and final ~3% over target, never within 3% of
    max; max (the budget per unit) stays here and is never put in Fo's prompt."""
    r = lambda x: round(x + 1e-9, 2)
    plan = {"their": their, "target": target, "max": max_unit, "open": r(target * 0.90), "best_final": r(target * 1.03)}
    if max_unit:
        plan["best_final"] = min(plan["best_final"], r(max_unit * 0.97))
    r1 = min(r(plan["open"] + (their - plan["open"]) / 2), plan["best_final"])  # half the gap to their price
    r2 = r((r1 + plan["best_final"]) / 2)  # later raises shrink; after these the next is best and final
    plan["raises"] = sorted({x for x in (r1, r2) if plan["open"] < x < plan["best_final"]})
    plan["trial"] = plan["raises"][0] if plan["raises"] else plan["best_final"]
    return plan


def decide(plan: dict, final: float | None) -> str:
    """After the call: at or under best and final -> accept; over it but under the hard line -> accept, flagged; over the hard line -> walk."""
    if final is None:
        return "no report: Fo never reported the call's outcome"
    if plan.get("max") and final > plan["max"]:
        return f"walked away: ${final:.2f} is over our ${plan['max']:.2f} per-unit hard line"
    if final > plan["best_final"]:
        if plan.get("max"):  # held high but still under the hard line: take it, flag it
            return f"accepted over our soft line ${plan['best_final']:.2f}"
        return f"walked away: ${final:.2f} is over our best and final ${plan['best_final']:.2f}"
    return "accepted"


def haggle_prompt(plan: dict, unit: str) -> str:
    """The moves Fo may say. The max is deliberately absent."""
    ladder = ", then ".join(f"${x:.2f}" for x in plan["raises"]) or "nothing in between"
    return (f"Open at ${plan['open']:.2f} per {unit}. Only raise after they drop their price, never before: {ladder}. "
            f"Trial close: ask 'if I get you to ${plan['trial']:.2f}, can you confirm today?'. If they still won't "
            f"move, your best and final is ${plan['best_final']:.2f} per {unit}; do not go above it. Do not commit or "
            "place an order. Report their final price and terms back.")


def flag_late(orders: list[dict]) -> list[dict]:
    today = date.today()
    for o in orders:  # late = past its ETA and not delivered yet; recomputed on every load and save
        o["late"] = o["status"] != "delivered" and date.fromisoformat(o["eta"]) < today
    return orders


def load_orders(path: pathlib.Path = ORDERS) -> list[dict]:
    return flag_late(json.loads(path.read_text())) if path.exists() else []


def save_orders(orders: list[dict], path: pathlib.Path = ORDERS):
    path.write_text(json.dumps(flag_late(orders), indent=1))


def add_order(spec: dict, q: dict, supplier: dict, path: pathlib.Path = ORDERS) -> str:
    orders = load_orders(path)
    today = date.today()
    orders.append({"po": f"PO-{len(orders) + 1:04d}", "supplier": supplier["name"], "supplier_id": supplier["id"],
                   "phone": supplier["phone"], "item": spec["what"], "quantity": spec["quantity"], "unit": spec["unit"],
                   "total": q["total"], "ordered": today.isoformat(),
                   "eta": (today + timedelta(days=q.get("lead_days") or 0)).isoformat(), "status": "ordered",
                   "history": [{"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "status": "ordered", "note": "PO sent"}]})
    save_orders(orders, path)
    return orders[-1]["po"]


def set_status(po: str, status: str, note: str = "", eta: str | None = None, path: pathlib.Path = ORDERS) -> dict:
    orders = load_orders(path)
    o = next((o for o in orders if o["po"] == po), None)
    if not o:
        raise SystemExit(f"no such order {po}; have {[x['po'] for x in orders]}")
    o["status"] = status
    if eta:
        o["eta"] = eta
    o["history"].append({"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "status": status, "note": note})
    save_orders(orders, path)
    return o


def parse_status(reply: str) -> dict:
    """Fo's summary of a status call -> {status, eta_days, note}."""
    j = llm_json("This is Fo's summary of a phone call asking a supplier where our order is. Reply with JSON only:\n"
                 '{"status":"ordered|shipped|delivered","eta_days":<days until delivery from today, or null>,'
                 '"note":"<tracking number, carrier, delay reason: one short line>"}\n'
                 f"ordered = not shipped yet. Only what the summary says.\n\nSummary: {reply}")
    if j and j.get("status") in ("ordered", "shipped", "delivered"):
        return {"status": j["status"], "eta_days": j.get("eta_days"), "note": j.get("note") or ""}
    low = reply.lower()
    status = "delivered" if "delivered" in low and "should be delivered" not in low else "shipped" if "shipped" in low else "ordered"
    m = re.search(r"in (\d+) days?", low)
    return {"status": status, "eta_days": int(m.group(1)) if m else None, "note": reply[:120]}


def followup(po: str, fo_mode: str = "stub", replies: dict = REPLIES_EXAMPLE, path: pathlib.Path = ORDERS) -> dict:
    """Fo calls the supplier's order desk and asks where PO is; the answer updates the tracker."""
    o = next((o for o in load_orders(path) if o["po"] == po), None)
    if not o:
        raise SystemExit(f"no such order {po}")
    fo = make_fo(fo_mode, replies)
    phone = o["phone"]
    if fo_mode != "stub":
        from fo import test_phone
        phone = test_phone()
    msg = (f"Please call {phone} and ask for {o['supplier']}'s order desk. Say you are calling for Buyline procurement "
           f"about the status of purchase order {po}: {o['quantity']} {o['unit']} of {o['item']}, ordered {o['ordered']}, "
           f"due {o['eta']}. Ask whether it has shipped, the carrier and tracking number, and the expected delivery "
           "date. Do not change the order. Report what they say back.")
    reply = fo.ask({"id": o["supplier_id"], "name": o["supplier"], "phone": phone}, msg)
    st = parse_status(reply)
    eta = (date.today() + timedelta(days=st["eta_days"])).isoformat() if st["eta_days"] is not None else None
    o = set_status(po, st["status"], f"Fo follow-up ({fo.mode}): {st['note'] or reply[:120]}", eta, path)
    o["followup"] = {"request": msg, "reply": reply}
    return o


# --- pipeline ---
def run(text: str, replies: dict = REPLIES_EXAMPLE, pause: float = 0, until: str = "ordered",
        out: pathlib.Path = HERE / "state.json", fo_mode: str = "stub", use_memory: bool = False,
        prerun: pathlib.Path | None = None, from_prerun: pathlib.Path | None = None,
        intake: str = "", photo: pathlib.Path | None = None, orders: pathlib.Path = ORDERS,
        rounds: int = 2, market=True, sourcing: str = "book", count="auto", max_suppliers: int | None = None,
        my_suppliers: list[dict] | None = None) -> dict:
    """sourcing: book (the replies file's suppliers, as before), ai (Claude finds them), mine (book + my_suppliers),
    both (book + my_suppliers + found). count: auto or a number; max_suppliers caps it (demo)."""
    fo = make_fo(fo_mode, replies)
    suppliers = replies.get("suppliers") or SUPPLIERS

    def to_test_targets(sups):  # dry and live only ever dial the teammates' test phones and email FO_TEST_EMAIL
        if fo_mode == "stub":
            return sups
        from fo import test_phone, test_email
        return [{**s, "phone": test_phone(s["id"]), "email": test_email()} for s in sups]
    suppliers = to_test_targets(suppliers)
    st = {"stage": "need", "fo": fo.mode, "spec": None, "suppliers": suppliers, "calls": [], "quotes": [],
          "recommendation": None, "po": None, "memory": None, "memory_on": use_memory, "market": None, "events": []}
    thinking = USE_LLM and sourcing != "book"  # the agent's Claude reasoning; the plain book run stays as it was

    lock = threading.Lock()

    def write():
        with lock:
            st["updated"] = datetime.now().strftime("%H:%M:%S")
            out.write_text(json.dumps(st, indent=1))

    def event(actor, kind, title, text="", chips=None, call_index=None):
        """One line on the dashboard timeline: agent thinks / Fo acts / you decide."""
        e = {"at": datetime.now().strftime("%H:%M:%S"), "actor": actor, "kind": kind, "title": title}
        if text:
            e["text"] = text
        if chips:
            e["chips"] = chips
        if call_index is not None:
            e["call_index"] = call_index
        with lock:
            st["events"].append(e)
        write()

    def reason(task, facts, fallback, secret=None):
        if not thinking:
            return fallback
        import agent
        return agent.think(task, facts, fallback, secret)

    def stage(name):
        st["stage"] = name
        write()
        time.sleep(pause)
        return STAGES.index(until) > STAGES.index(name)

    def call(s, kind, msg):
        """One Fo conversation; its card is on the dashboard from the first second, status updated per poll."""
        msg += HANGUP  # Fo otherwise stays on the line after 'thank you, goodbye'
        c = {"supplier": s["id"], "name": s["name"], "kind": kind, "request": msg, "transcript": None,
             "phone": "···" + s["phone"].replace(" ", "")[-4:],  # masked for the dashboard: last 4 digits only
             "status": "queued", "at": datetime.now().strftime("%H:%M")}
        if fo.mode == "sample":
            c["channel"] = "sample"  # the dashboard labels it 'sample call'
        with lock:
            st["calls"].append(c)
            idx = len(st["calls"]) - 1
        event("fo", "act", f"{'Negotiating with' if kind == 'negotiate' else 'Calling'} {s['name']}", call_index=idx)

        def status(x):
            c["status"] = x
            write()
        tr = fo.ask(s, msg, status)
        c.update(transcript=tr, status="no answer" if NO_ANSWER.search(tr.split(SEP)[-1]) else
                 "counter in" if kind == "negotiate" else "quote in",
                 reports=[x.strip() for x in tr.split(SEP)])  # one Fo report per call when Fo rang back
        write()
        return tr

    def quote_call(s):
        tr = call(s, "quote", f"Please call {s['phone']} and ask for {s['name']}'s sales desk. Say you are calling for "
                              f"Buyline procurement and need a quote on {spec['quantity']} {spec['unit']} of {what}"
                              + (f", delivered within {spec['lead_days']} days" if spec.get("lead_days") else "")
                              + ". Ask for: unit price, minimum order quantity, lead time, shipping cost, any fees, "
                                "and import duty if it ships from abroad. "
                              + (f"Our records: {known[s['id']]} Ask them to match or beat their last price. "
                                 if s["id"] in known else "")
                              + "Do not commit to anything or place an order. Report the exact numbers back.")
        q = {"id": s["id"], "name": s["name"], **parse_quote(tr, spec)}
        q["total"] = total(q, spec)
        with lock:
            st["quotes"].append(q)
        write()

    write()
    spec = st["spec"] = parse_spec(text, intake, photo)
    spec["intake"] = intake[:400]
    spec["photo"] = str(photo) if photo else None
    size = spec.get("size")
    if size and size.lower().replace(" ", "") in spec["item"].lower().replace(" ", ""):
        size = None  # the model put the size inside the item already
    what = " ".join(x for x in (size, spec["item"]) if x) + \
        (f" ({', '.join(spec['features'])})" if spec.get("features") else "")
    spec["what"] = what
    if fo.mode == "sample":  # the public link: canned calls written for this item, never a phone
        fo.replies = sample_replies(spec)
    pre = json.loads(from_prerun.read_text()) if from_prerun and from_prerun.exists() else None
    if pre and pre.get("market"):
        market = lambda _spec, m=pre["market"]: m  # prices swing between runs: the demo reuses the saved check

    def market_job():  # ~20 s of web search, run beside the quote calls so it adds no wait
        res = market_check(spec, market)
        st["market"] = res
        write()
    mt = threading.Thread(target=market_job, daemon=True)
    mt.start()
    event("agent", "result", "Understood the request",
          "Read the uploaded " + ("photo" if photo else "document") + "." if (photo or intake) else "From your typed request.",
          chips=[["Item", what], ["Qty", f"{spec['quantity']:,} {spec['unit']}"]]
          + ([["Within", f"{spec['lead_days']} days"]] if spec.get("lead_days") else [])
          + ([["Budget", f"${spec['budget']:,.0f}"]] if spec.get("budget") else [])
          + ([["Ship to", spec["ship_to"]]] if spec.get("ship_to") else []))
    if not stage("need") or not stage("suppliers"):
        mt.join()
        return st
    if pre and pre.get("sourcing"):  # the demo replays the suppliers its pre-run quotes came from
        st["sourcing"] = pre["sourcing"]
        suppliers = to_test_targets(pre["suppliers"])
    elif sourcing != "book" and not pre:  # a pre-run without saved sourcing replays its own suppliers
        import agent
        book = [dict(s) for s in (replies.get("suppliers") or SUPPLIERS)] if sourcing in ("mine", "both") else []
        book += [dict(s, source="added") for s in (my_suppliers or [])]
        cap = max_suppliers
        if fo_mode == "stub":  # canned replies exist only for the replies file's supplier ids
            cap = min(cap or 99, len(replies.get("quotes") or {}))
        event("agent", "think", "Looking for suppliers",
              {"ai": "Searching the web for businesses that stock this item and deliver to you.",
               "mine": "Using your supplier book.",
               "both": "Searching the web, and including the suppliers in your book."}[sourcing])
        src = agent.source(spec, "ai" if sourcing == "ai" else sourcing, count, book, cap)
        short = [c for c in src["found"] if c["status"] == "shortlisted"]
        names = {s["id"]: s["name"] for s in (replies.get("suppliers") or SUPPLIERS)}
        if fo_mode == "stub":  # the canned transcripts name the replies file's suppliers: say the real names instead
            replies = json.loads(json.dumps(replies))
            for c in short:
                for k in ("quotes", "negotiate"):
                    if isinstance(replies.get(k), dict) and c["id"] in replies[k]:
                        replies[k][c["id"]] = replies[k][c["id"]].replace(names.get(c["id"], "\0"), c["name"])
            fo.replies = replies
        suppliers = to_test_targets([{"id": c["id"], "name": c["name"], "phone": c.get("phone") or c.get("listed_phone") or "",
                                      "email": c.get("email") or c.get("listed_email") or ""} for c in short])
        found_n, ai_n = len(src["found"]), sum(c["source"] == "ai" for c in src["found"])
        src["reason"] = reason(
            "Explain to the buyer how we chose which suppliers to contact and why this many.",
            {"found": [{"name": c["name"], "source": c["source"], "score": c.get("score"), "status": c["status"],
                        "why": c["why"]} for c in src["found"]], "count_rule": src["count_why"],
             "search_error": src["error"]},
            (f"Found {ai_n} candidates on the web" if ai_n else "Used your supplier book")
            + f"; contacting {len(short)} ({src['count_why']})." + (f" Search problem: {src['error']}." if src["error"] else ""))
        st["sourcing"] = src
        event("agent", "result", f"Found {found_n}, contacting {len(short)}" if ai_n else f"Contacting {len(short)} suppliers",
              src["reason"], chips=[[None, c["name"]] for c in short])
    st["suppliers"] = suppliers
    write()
    known: dict[str, str] = {}
    if use_memory:  # what Cognee remembers about these suppliers and this product, before anyone is called
        import memory  # noqa: F811 (lazy: cognee import is slow)
        iw = {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", what.lower()) if len(w) > 2}
        # only facts about this product: Cognee's nearest chunks for coffee bags can still be copper wire
        q = f"past quotes and deals for {what} from suppliers"
        rec = memory.recall(q)
        if not rec or rec[0].startswith("cognee error"):  # the local store flakes now and then: one retry
            print("memory:", rec, "- retrying", file=sys.stderr)
            rec = memory.recall(q)
        st["memory"] = [m for m in rec if m.startswith("cognee error")
                        or iw & {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", m.lower())}]
        for s in suppliers:
            hits = [m for m in st["memory"] if s["name"].lower() in m.lower()]
            if hits:
                known[s["id"]] = hits[0]
        write()
    if not stage("calling"):
        return st
    if pre:  # quote calls done ahead of the demo, replayed one at a time at call pace; only the negotiation is live
        quotes = {q["id"]: q for q in pre["quotes"]}
        for c in pre["calls"]:
            c["status"] = "queued"
            st["calls"].append(c)
            event("fo", "act", f"Calling {c['name']}", call_index=len(st["calls"]) - 1)
            time.sleep(2)
            for status in ("dialing", "on the phone"):
                c["status"] = status
                write()
                time.sleep(CALL_SECS)
            if c["supplier"] in quotes:
                st["quotes"].append(quotes[c["supplier"]])
            c["status"] = "quote in"
            write()
        st["quotes"].sort(key=lambda q: q["id"])
    else:  # all suppliers at once, one Fo conversation each; one at a time when they share a phone (one teammate)
        phones = [s["phone"] for s in suppliers]
        workers = max(len(suppliers), 1) if len(set(phones)) == len(phones) else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(quote_call, suppliers))
        st["quotes"].sort(key=lambda q: q["id"])
    st["recommendation"] = recommend(st["quotes"], spec)
    mt.join()
    if prerun:
        prerun.write_text(json.dumps({"request": text, "calls": st["calls"], "quotes": st["quotes"],
                                      "market": st["market"], "sourcing": st.get("sourcing"),
                                      "suppliers": [{k: s[k] for k in ("id", "name") if k in s} for s in suppliers]}, indent=1))
        print(f"pre-run saved to {prerun}: {len(st['quotes'])} quotes", file=sys.stderr)
        until = "quotes"
    if not stage("quotes"):
        return st
    rec = st["recommendation"]["supplier"]
    qfacts = [{"name": q["name"], "unit_price": q["unit_price"], "min_order": q["min_order"], "lead_days": q["lead_days"],
               "shipping": q.get("shipping"), "landed_total": q["total"]} for q in st["quotes"]]
    event("agent", "think", "Compared the quotes on landed cost",
          reason("Compare these quotes for the buyer. Judge on landed total (what we would actually pay, including any "
                 "minimum order), the deadline and the budget, and say which ones are ruled out and why.",
                 {"need": {"quantity": spec["quantity"], "unit": spec["unit"], "deadline_days": spec.get("lead_days"),
                           "budget": spec.get("budget")}, "quotes": qfacts, "code_verdict": st["recommendation"]["why"]},
                 " ".join(st["recommendation"]["why"])))
    if not stage("negotiate"):
        return st
    mk = {k: v for k, v in (st["market"] or {}).items() if k in ("soft_line", "hard_line") and v}
    best_px = min((q["unit_price"] for q in st["quotes"] if q["unit_price"]), default=None)
    typ = (st["market"] or {}).get("typical")
    if mk and best_px and typ and not (0.5 * best_px <= typ <= 2 * best_px):  # web prices for another unit or grade: not a lever
        mk = {}
        st["market"]["line"] += f" · not used for the haggle (quotes are around ${best_px:.2f})"
        write()
    unit = spec["unit"].rstrip("s")
    per_unit = spec["budget"] / spec["quantity"] if spec.get("budget") else None
    hard = min(x for x in (per_unit, mk.get("hard_line")) if x) if (per_unit or mk.get("hard_line")) else None
    caps = [x for x in (spec.get("target_price"), mk.get("soft_line")) if x]  # user target and market soft line
    if hard:
        caps = [min(x, hard) for x in caps]

    def fits(x):  # a runner-up can only beat the best if it can actually fill this order
        return (x["unit_price"] is not None and x["min_order"] <= spec["quantity"]
                and not (spec.get("lead_days") and x["lead_days"] and x["lead_days"] > spec["lead_days"]))

    st["rounds"], notes = [], []  # recommend() rebuilds its why-lines each round, so the rounds' own lines are kept here
    strategy_why = None
    for rnd in range(1, rounds + 1):
        rec = st["recommendation"]["supplier"]
        if not rec:
            break
        best = next(x for x in st["quotes"] if x["id"] == rec)
        if rnd == 1:  # lever: the lowest unit price anyone quoted (or what we last paid), taken to the supplier we'd buy from
            q = best
            rivals = [x for x in st["quotes"] if x is not q and x["unit_price"] is not None]
            lever = min(rivals, key=lambda x: x["unit_price"]) if rivals else None
            paid = last_paid(st["memory"] or [])  # what we actually paid last time, from Cognee
            if paid and paid[0] < q["unit_price"] and (not lever or paid[0] <= lever["unit_price"]):
                lever = {"name": paid[1], "unit_price": paid[0], "kind": "last paid", "terms": "our last order with them"}
            if thinking:  # Claude picks who to push and with which real lever; code checks the pick and sets the numbers
                import agent
                targets = [x for x in st["quotes"] if fits(x) and x["total"] is not None
                           and (not spec.get("budget") or x["total"] <= spec["budget"])]
                levers = [{**x, "terms": quote_terms(x, spec["unit"])} for x in st["quotes"] if x["unit_price"] is not None]
                if paid:
                    levers.append({"id": "paid", "name": paid[1], "unit_price": paid[0], "kind": "last paid",
                                   "terms": "our last order with them"})
                pick = agent.choose_round1(
                    [{k: x[k] for k in ("id", "name", "unit_price", "total", "min_order", "lead_days")} for x in targets],
                    [{"id": x["id"], "name": x["name"], "unit_price": x["unit_price"], "terms": x["terms"]} for x in levers])
                if pick:
                    q = next(x for x in targets if x["id"] == pick["target_id"])
                    lever = next(x for x in levers if x["id"] == pick["lever_id"])
                    strategy_why = pick["why"]
        else:  # callback round: the runner-up that can fill the order hears the best offer and may beat it
            others = [x for x in st["quotes"] if x is not best and fits(x)]
            q = min(others, key=lambda x: x["unit_price"]) if others else None
            lever = best
        if not q or not lever:
            break
        if "terms" not in lever:
            lever = {**lever, "terms": quote_terms(lever, spec["unit"])}
        tgt = min([lever["unit_price"], *caps])
        if tgt >= q["unit_price"]:
            break  # nothing to push for
        target = next(s for s in suppliers if s["id"] == q["id"])
        plan = haggle(q["unit_price"], tgt, hard)
        plan.update(round=rnd, supplier=q["name"], your_target=spec.get("target_price"))
        opener = (f"Say Buyline procurement last paid ${lever['unit_price']:.2f} per {unit} "
                  f"for the same {what} (from {lever['name']})" if lever.get("kind") == "last paid" else
                  f"Say Buyline procurement has another supplier who offered ${lever['unit_price']:.2f} per "
                  f"{unit} for the same {what}")
        msg = (f"Please call {target['phone']} back and ask for {target['name']}'s sales desk. {opener}, and "
               f"ask if they can beat that for {spec['quantity']} {spec['unit']}. The other offer's terms: "
               f"{lever['terms']}. If asked about the other offer, answer truthfully with only these facts; never "
               f"invent a supplier, price or term. {haggle_prompt(plan, unit)}")
        # the rival's real quote may equal our hard line; the leak check is on the moves code wrote, not on facts
        assert not plan["max"] or f"${plan['max']:.2f}" not in haggle_prompt(plan, unit), "the ceiling must never reach Fo"
        pfacts = {"round": rnd, "supplier": q["name"], "their_price": q["unit_price"], "unit": unit,
                  "lever": {"from": lever["name"], "unit_price": lever["unit_price"], "terms": lever["terms"],
                            "kind": lever.get("kind") or "rival quote"},
                  "open_at": plan["open"], "raises": plan["raises"], "trial_close": plan["trial"],
                  "best_and_final": plan["best_final"], "why_this_supplier": strategy_why if rnd == 1 else
                  "runner-up that can fill the order; we take the best offer back to them"}
        event("agent", "think", f"Round {rnd}: plan for {q['name']}",
              reason("Explain the negotiation plan for this round: why this supplier, why this lever, and how we will "
                     "move. Do not mention any walk-away or maximum price.", pfacts,
                     (strategy_why + " " if rnd == 1 and strategy_why else "")
                     + f"Lever: {lever['name']}'s ${lever['unit_price']:.2f} ({lever['terms']}). Open at ${plan['open']:.2f}, "
                       f"raise only after they move, best and final ${plan['best_final']:.2f}. Our walk-away price stays here.",
                     secret=plan["max"]))
        tr = call(target, "negotiate", msg)
        # Fo's "call's going out" ack quotes our own ladder: never a price source
        last = tr.split(SEP)[-1]  # ack + report(s) in one conversation: judge only Fo's last report
        new = parse_quote(tr, spec) if not PENDING.search(last) else {"unit_price": None}
        final = new["unit_price"] if new["unit_price"] else (None if PENDING.search(last) else q["unit_price"])
        plan["final"], plan["result"] = final, decide(plan, final)
        plan["line"] = (f"Opened ${plan['open']:.2f}"
                        + (f" · your target ${spec['target_price']:.2f}" if spec.get("target_price") else "")
                        + f" · soft line ${plan['best_final']:.2f}"
                        + (f" · hard line ${plan['max']:.2f}" if plan["max"] else "")
                        + (f" · their final ${final:.2f}" if final else " · no final price") + " · " + plan["result"].split(":")[0])
        st["haggle"] = plan
        st["rounds"].append(plan)
        was = q["unit_price"]
        if final and final < q["unit_price"] and plan["result"].startswith("accepted"):
            q.setdefault("first_price", q["unit_price"])
            q.update({k: v for k, v in new.items() if v not in (None, 0, [])})  # silence keeps old terms
            q["total"] = total(q, spec)
        st["recommendation"] = recommend(st["quotes"], spec, haggled=True)
        against = (f"the ${lever['unit_price']:.2f} we last paid {lever['name']} (Cognee memory)"
                   if lever.get("kind") == "last paid" else f"{lever['name']}'s ${lever['unit_price']:.2f}")
        notes.append(
            f"Negotiated {target['name']} from ${was:.2f} to ${q['unit_price']:.2f}, opening at "
            f"${plan['open']:.2f} against {against}" if q["unit_price"] < was else
            f"{target['name']} held at ${final:.2f} after we opened at ${plan['open']:.2f} against {against}" if final else
            f"{target['name']}: Fo never reported the call's outcome, so no price was taken from the call")
        if not plan["result"].startswith("accepted"):  # code, not Fo, decides: over the hard line means no order
            notes.append(f"{target['name']}: {plan['result']}")
            if st["recommendation"]["supplier"] == q["id"]:
                st["recommendation"]["supplier"] = None
        st["recommendation"]["why"].extend(notes)
        event("agent", "result", notes[-1] if plan["result"].startswith("accepted") else f"{target['name']}: {plan['result']}",
              chips=[["Opened", f"${plan['open']:.2f}"]] + ([["Their final", f"${final:.2f}", "good" if final < was else None]]
                                                             if final else []))
        if rnd > 1 and st["recommendation"]["supplier"] == rec:
            event("agent", "think", "Nobody improved, so we stop here",
                  "Another round would only cost time; the current best offer stands.")
            break  # nobody improved: stop calling
    rq = next((x for x in st["quotes"] if x["id"] == st["recommendation"]["supplier"]), None)
    if rq:
        event("agent", "ask", f"We recommend {rq['name']} at ${rq['total']:,.2f}. Your call.",
              reason("Summarize the recommendation for the buyer in plain words: who, the landed total, and what the "
                     "negotiation and the traps we avoided saved.",
                     {"pick": {"name": rq["name"], "landed_total": rq["total"], "unit_price": rq["unit_price"],
                               "first_price": rq.get("first_price"), "lead_days": rq["lead_days"]},
                      "budget": spec.get("budget"), "quotes": qfacts, "notes": st["recommendation"]["why"]},
                     " ".join(st["recommendation"]["why"][:2])))
    else:
        event("agent", "result", "No supplier fits every constraint", " ".join(st["recommendation"]["why"]))
    import briefing  # morning briefing in Orpheus (Deepgram) when DEEPGRAM_API_KEY is set, else text only
    st["briefing"] = briefing.brief(st, out.with_name("briefing.mp3"))
    if not stage("approve"):
        return st
    place_order(st, fo, out, orders, use_memory)
    return st


def save_state(st: dict, out: pathlib.Path):
    st["updated"] = datetime.now().strftime("%H:%M:%S")
    out.write_text(json.dumps(st, indent=1))


def place_order(st: dict, fo, out: pathlib.Path, orders: pathlib.Path = ORDERS, use_memory: bool = False) -> dict:
    """approve -> ordered from the state in hand: PO body, PO email, tracker row, briefing, Cognee memory. No Fo call."""
    import briefing
    spec, what, rec = st["spec"], st["spec"]["what"], st["recommendation"]["supplier"]
    if rec:
        target = next(s for s in st["suppliers"] if s["id"] == rec)
        q = next(q for q in st["quotes"] if q["id"] == rec)
        body = (f"Purchase order to {target['name']}: {spec['quantity']} {spec['unit']} of {what} at "
                f"${q['unit_price']:.2f} per {spec['unit'].rstrip('s')}, total ${q['total']:.2f}"
                + (f" including ${q['shipping']:.2f} shipping" if q.get("shipping") else "")
                + f", delivery within {q['lead_days']} days"
                + (f", ship to {spec['ship_to']}" if spec.get("ship_to") else "") + ".")
        st["po"] = {"supplier": rec, "body": body, "sent": fo.email(target.get("email") or target["name"], "Purchase order", body),
                    "number": add_order(spec, q, target, orders),
                    "to": re.sub(r"^(.).*@", r"\1***@", target.get("email") or "")}  # masked inbox: s***@gmail.com
        at = datetime.now().strftime("%H:%M:%S")
        st.setdefault("events", []).extend([
            {"at": at, "actor": "you", "kind": "result", "title": f"Approved {target['name']} at ${q['total']:,.2f}"},
            {"at": at, "actor": "fo", "kind": "act", "channel": "email", "title": f"Purchase order {st['po']['number']} to {target['name']}",
             "text": st["po"]["sent"]}])
        st["stage"] = "ordered"
        save_state(st, out)
        st["briefing"] = briefing.brief(st, out.with_name("briefing.mp3"))  # now says the PO went out
    if use_memory and st["po"]:  # remember every quote and the deal, so the next order starts from here
        day = datetime.now().strftime("%Y-%m-%d")
        lines = [f"On {day} {q['name']} quoted ${q.get('first_price', q['unit_price']):.2f} per "
                 f"{spec['unit'].rstrip('s')} for {what}, minimum order {q['min_order']}, lead time {q['lead_days']} "
                 f"days" + (f", shipping ${q['shipping']:.2f}" if q.get("shipping") else ", shipping included")
                 + (f", fees ${q['fees']:.2f}" if q.get("fees") else "") + (f", duty {q['duty']}" if q.get("duty") else "")
                 + (f"; negotiated down to ${q['unit_price']:.2f}" if q.get("first_price") else "")
                 + (f". We ordered from {q['name']}." if q["id"] == rec else ".") for q in st["quotes"]
                 if q["unit_price"] is not None]
        import memory as mem  # lazy: approve() runs in serve.py's process, where run() never imported it
        st["memory_saved"] = mem.remember(lines)
    save_state(st, out)
    return st


def approve(out: pathlib.Path = HERE / "state.json", orders: pathlib.Path = ORDERS, yes: bool = True,
            supplier: str | None = None) -> dict:
    """The dashboard's Approve / Not now buttons (serve.py). Finishes the saved run; never calls Fo again.
    supplier: the buyer's own pick instead of the recommendation, taken only if that quote fits every constraint."""
    st = json.loads(out.read_text())
    if st["stage"] != "approve" or not (st.get("recommendation") or {}).get("supplier"):
        return st
    if yes and supplier and supplier != st["recommendation"]["supplier"]:
        spec, q = st["spec"], next((x for x in st["quotes"] if x["id"] == supplier), None)
        ok = (q and q["total"] is not None and q["min_order"] <= spec["quantity"]
              and not (spec.get("lead_days") and q["lead_days"] and q["lead_days"] > spec["lead_days"])
              and not (spec.get("budget") and q["total"] > spec["budget"]))
        if not ok:
            st["error"] = f"{supplier} doesn't fit the order (minimum order, deadline or budget)"
            return st
        st["recommendation"]["supplier"] = supplier
        st["recommendation"]["why"].append(f"You chose {q['name']} over our recommendation.")
    if not yes:
        st["decision"] = "not now"
        save_state(st, out)
        return st
    st["decision"] = "approved"
    return place_order(st, make_fo("stub", REPLIES_EXAMPLE), out, orders, st.get("memory_on", False))


def check():
    global USE_LLM
    USE_LLM = False  # deterministic path
    oc, sc = HERE / f"orders.check.{os.getpid()}.json", HERE / f"state.check.{os.getpid()}.json"  # per process: two checks at once must not collide
    oc.unlink(missing_ok=True)
    fake = lambda spec: {"low": 0.70, "typical": 0.85, "high": 1.10, "sources": ["https://example.com/wire"],
                         "soft_line": 0.84, "hard_line": 1.05}
    st = run("500 m of 12AWG copper wire, within two weeks, budget $500, ship to Berkeley CA",
             out=sc, orders=oc, market=fake)
    first = load_orders(oc)
    st2 = run("500 m of 12AWG copper wire, within two weeks, budget $500, target price $0.75 per meter, ship to Berkeley CA",
              out=sc, orders=oc, until="approve", market=lambda spec: {"error": "offline"})
    assert st2["stage"] == "approve" and st2["po"] is None
    assert st2["spec"]["target_price"] == 0.75 and st2["market"]["line"].startswith("Market check unavailable (offline)"), st2["market"]
    h2 = st2["rounds"][0]  # user target 0.75 under B's 0.80: soft line 0.77; C's 0.78 is over it but under the hard line
    assert (h2["target"], h2["best_final"], h2["final"], h2["result"]) == (0.75, 0.77, 0.78, "accepted over our soft line $0.77"), h2
    assert "your target $0.75" in h2["line"] and st2["recommendation"]["supplier"] == "C", h2["line"]
    assert approve(sc, oc, yes=False)["decision"] == "not now"
    assert json.loads((sc).read_text())["stage"] == "approve"
    st2 = approve(sc, oc)
    assert st2["stage"] == "ordered" and st2["po"]["number"] == "PO-0002" and "went out" in st2["briefing"]["text"], st2["po"]
    save_orders(first, oc)  # back to the first run's PO-0001 for the tracker checks below
    sc.unlink()
    sp = st["spec"]
    assert (sp["quantity"], sp["unit"], sp["budget"], sp["lead_days"], sp["ship_to"]) == (500, "meters", 500, 14, "Berkeley CA"), sp
    assert sp["item"].lower().startswith("12awg copper wire"), sp
    by = {q["id"]: q for q in st["quotes"]}
    assert len(by) == 3 and by["B"]["min_order"] == 1000 and by["B"]["total"] == 800.0, by
    assert st["recommendation"]["supplier"] == "C", st["recommendation"]
    assert any("minimum order 1000" in w for w in st["recommendation"]["why"]), st["recommendation"]
    assert by["C"]["unit_price"] == 0.78 and by["C"]["total"] == 390.0, by["C"]
    assert st["stage"] == "ordered" and st["po"]["supplier"] == "C" and "Berkeley" in st["po"]["body"]
    assert "called 3 suppliers" in st["briefing"]["text"] and "Supplier C at $390.00" in st["briefing"]["text"], st["briefing"]
    assert "0.80" not in st["po"]["body"] and "0.88" not in st["po"]["body"], st["po"]
    assert "went out" in st["briefing"]["text"] and "approval" not in st["briefing"]["text"], st["briefing"]
    o = load_orders(oc)[0]
    assert o["po"] == "PO-0001" and o["status"] == "ordered" and not o["late"], o
    o["eta"] = "2020-01-01"; o["late"] = False
    oc.write_text(json.dumps([o]))  # stale file: late must be recomputed on load, not only on save
    assert load_orders(oc)[0]["late"] is True
    o["eta"] = str(date.today() + timedelta(days=7)); oc.write_text(json.dumps([o]))
    assert date.fromisoformat(o["eta"]) == date.today() + timedelta(days=7), o
    o = followup("PO-0001", path=oc)
    assert o["status"] == "shipped" and date.fromisoformat(o["eta"]) == date.today() + timedelta(days=3), o
    assert set_status("PO-0001", "delivered", path=oc)["status"] == "delivered"
    oc.unlink()
    sp2 = {"size": "12 oz", "item": "12 oz kraft coffee bags", "features": []}
    sz = sp2["size"] if sp2["size"].lower().replace(" ", "") not in sp2["item"].lower().replace(" ", "") else None
    assert " ".join(x for x in (sz, sp2["item"]) if x) == "12 oz kraft coffee bags"
    assert st["market"]["line"] == "Market check: $0.70-$1.10 per meter, typical $0.85", st["market"]  # budget 1.00/m is above typical
    assert [r["supplier"] for r in st["rounds"]] == ["Supplier C", "Supplier A"], st["rounds"]  # round 2: runner-up A hears C's 0.78
    h, h1 = st["haggle"], st["rounds"][0]
    assert (h1["open"], h1["best_final"], h1["trial"], h1["result"]) == (0.72, 0.82, 0.80, "accepted"), h1  # lever: B's 0.80
    assert h1["line"] == "Opened $0.72 · soft line $0.82 · hard line $1.00 · their final $0.78 · accepted", h1["line"]
    assert h["target"] == 0.78 and h["final"] == 0.95 and h["result"].startswith("accepted over our soft line"), h  # A held, C stays
    assert any("Supplier A held at $0.95" in w for w in st["recommendation"]["why"]), st["recommendation"]["why"]
    assert len(st["calls"]) == 5 and sum(c["kind"] == "negotiate" for c in st["calls"]) == 2
    from fo import PENDING
    ack = "Call's going out now. It opens at $0.70, holds $0.80 as the hard ceiling. I'll bring you their final price once it wraps."
    assert PENDING.search(ack) and not PENDING.search("They agreed to $0.78 per meter, lead time 7 days."), ack
    cq = [{"id": "C", "name": "C", "total": 440.0, "unit_price": 0.88, "min_order": 200, "lead_days": 7}]
    assert recommend(cq, {"quantity": 500, "unit": "meters", "budget": 425})["supplier"] == "C"  # negotiable before haggling
    assert recommend(cq, {"quantity": 500, "unit": "meters", "budget": 425}, haggled=True)["supplier"] is None
    neg, neg2 = [c["request"] for c in st["calls"] if c["kind"] == "negotiate"]
    assert "offered $0.78" in neg2 and "minimum order 200 meters" in neg2 and "Supplier A" in neg2, neg2
    assert "minimum order 1000 meters" in neg and "answer truthfully" in neg, neg
    assert "orders@supplier-c.example" in st["po"]["sent"], st["po"]
    assert st["po"]["to"] == "o***@supplier-c.example" and st["calls"][0]["phone"] == "···0001", st["po"]
    assert "$0.72" in neg and "$0.82" in neg and "$1.00" not in neg and "budget" not in neg.lower(), neg  # max never reaches Fo
    plan = haggle(0.88, 0.78, 1.00)
    assert decide(plan, 0.78) == "accepted" and decide(plan, 0.80) == "accepted", plan
    assert decide(plan, 0.90) == "accepted over our soft line $0.80" and decide(plan, 1.20).endswith("hard line"), plan
    assert decide(haggle(0.88, 0.78), 0.90).startswith("walked away")  # no hard line: best and final is the line
    assert haggle(0.88, 0.78, 0.80)["best_final"] == 0.78  # never within 3% of max
    assert decide(plan, None).startswith("no report")
    assert all(c["request"].endswith(HANGUP) for c in st["calls"]), st["calls"][0]["request"]
    two = SEP.join(["Their floor landed at $0.82, above your $0.80 ceiling. I'm going back once to put it on the table.",
                    "Supplier C settled at $0.80 per meter for 500 meters. Terms: free shipping, 7-day lead time, minimum order 200 meters."])
    assert PENDING.search(two.split(SEP)[0]) and not PENDING.search(two.split(SEP)[1])
    assert parse_quote(two, sp)["unit_price"] == 0.80, parse_quote(two, sp)
    assert PENDING.search(two) and not PENDING.search(two.split(SEP)[-1])
    assert all(NO_ANSWER.search(x) for x in ("Fo never reported the call's outcome: hi", "Fo call failed: could not read",
               "The call connected but the person hung up without saying anything.")) and not NO_ANSWER.search(two)  # the ack must not hide the report
    print("check ok: haggle open 0.70 / best-final 0.80, 0.78 accepted, 0.90 walked away, max absent from Fo prompt")
    sr = sample_replies({"quantity": 200, "unit": "bars", "budget": 1000})
    assert "$5.50 per bar, minimum order 80 bars" in sr["quotes"]["C"] and "minimum order 400 bars" in sr["quotes"]["B"], sr
    assert parse_quote(sr["negotiate"]["C"], {"quantity": 200, "unit": "bars"})["unit_price"] == 4.88, sr["negotiate"]
    ms = parse_spec("Falcon build, budget $900\n\nFrom the attached document:\n1. 500 m 12AWG copper wire\n2. 200 pcs M8 hex bolts\n3. 12 rolls electrical tape")
    assert [i["name"] for i in ms["items"]] == ["12AWG copper wire", "M8 hex bolts", "electrical tape"] and ms["item"] == "12AWG copper wire" \
        and ms["quantity"] == 500 and ms["queued_next"] == ["200 pieces M8 hex bolts", "12 rolls electrical tape"], ms
    print("check ok: C picked over B (min order 1000 > 500), negotiated 0.88 -> 0.78, PO to C, "
          "PO-0001 tracked ordered -> shipped (ETA from Fo follow-up) -> delivered")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("request", nargs="?", default="500 m of 12AWG copper wire, within two weeks, budget $500, ship to Berkeley CA")
    ap.add_argument("--replies", help="JSON file of canned Fo replies (see REPLIES_EXAMPLE)")
    ap.add_argument("--pause", type=float, default=0, help="seconds between stages, so the dashboard animates")
    ap.add_argument("--until", default="ordered", choices=STAGES)
    ap.add_argument("--out", default=str(HERE / "state.json"))
    ap.add_argument("--no-llm", action="store_true", help="regex extraction only")
    ap.add_argument("--fo", default="stub", choices=["stub", "sample", "dry", "live"],
                    help="live places real Fo calls to FO_TEST_PHONE: only after the human's yes")
    ap.add_argument("--no-memory", action="store_true", help="skip Cognee supplier memory (faster)")
    ap.add_argument("--intake", help="file with an old invoice, spec sheet or email to reorder from ('-' = stdin)")
    ap.add_argument("--photo", help="photo of an invoice, label or the item itself")
    ap.add_argument("--prerun", action="store_true", help="run only the quote calls and save them to prerun.json")
    ap.add_argument("--from-prerun", action="store_true", help="load prerun.json; only the negotiation call is live")
    ap.add_argument("--rounds", type=int, default=2, help="haggle rounds: 1 = only the best supplier; 2 = then the runner-up with the best offer as lever")
    ap.add_argument("--no-market", action="store_true", help="skip the web-search market check")
    ap.add_argument("--approve", action="store_true", help="finish the saved state.json approve -> ordered: sends the PO, makes no calls")
    ap.add_argument("--not-now", action="store_true", help="leave the saved state.json at approve, marked 'not now'")
    ap.add_argument("--status", nargs=2, metavar=("PO", "STATE"), help="mark an order ordered|shipped|delivered")
    ap.add_argument("--followup", metavar="PO", help="Fo calls the supplier for this order's status (dry/stub only)")
    ap.add_argument("--sourcing", default="book", choices=["book", "ai", "mine", "both"],
                    help="book: the replies file's suppliers (default); ai: Claude finds suppliers on the web; "
                         "mine: your supplier book; both: book plus found")
    ap.add_argument("--count", default="auto", help="how many suppliers to contact: auto or a number")
    ap.add_argument("--max-suppliers", type=int, default=int(os.environ.get("BUYLINE_MAX_SUPPLIERS") or 0) or None,
                    help="hard cap on suppliers contacted (demo); env BUYLINE_MAX_SUPPLIERS")
    ap.add_argument("--my-suppliers", help='JSON list of extra suppliers: [{"name": ..., "phone": ..., "email": ...}]')
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
        sys.exit()
    USE_LLM = not a.no_llm
    replies = json.loads(pathlib.Path(a.replies).read_text()) if a.replies else REPLIES_EXAMPLE
    if a.status:
        print(json.dumps(set_status(a.status[0], a.status[1], "marked by hand"), indent=1))
        sys.exit()
    if a.followup:
        print(json.dumps(followup(a.followup, a.fo, replies), indent=1))
        sys.exit()
    if a.approve or a.not_now:
        st = approve(pathlib.Path(a.out), yes=a.approve)
        print(f"{st['stage']}: {st.get('decision', 'nothing waiting for approval')}" + (f", {st['po']['number']} sent" if st.get("po") else ""))
        sys.exit()
    st = run(a.request, replies, a.pause, a.until, pathlib.Path(a.out), a.fo, not a.no_memory,
             HERE / "prerun.json" if a.prerun else None, HERE / "prerun.json" if a.from_prerun else None,
             (sys.stdin.read() if a.intake == "-" else pathlib.Path(a.intake).read_text()) if a.intake else "",
             pathlib.Path(a.photo) if a.photo else None, rounds=a.rounds, market=not a.no_market,
             sourcing=a.sourcing, count=a.count, max_suppliers=a.max_suppliers,
             my_suppliers=json.loads(a.my_suppliers) if a.my_suppliers else None)
    print(json.dumps({"stage": st["stage"], "spec": st["spec"], "memory": st["memory"], "market": st["market"],
                      "recommendation": st["recommendation"], "po": st["po"], "memory_saved": st.get("memory_saved")},
                     indent=1))
