#!/usr/bin/env python3
"""Buyline's thinking agent (Claude): supplier discovery with web search, scoring and shortlist size, negotiation
strategy, and plain-language reasoning for the dashboard timeline.

Division of labour: Claude researches, chooses among options that code built, and explains. Code owns every price:
scores, the shortlist cap, haggle() numbers and the walk-away line. Claude never sees the walk-away line, and any
reasoning text that quotes a dollar figure not in its facts is thrown away for the code-written fallback.

  self-check (live Claude + web search): ../.venv/bin/python agent.py"""
import json, os, pathlib, re, string, sys, time

HERE = pathlib.Path(__file__).resolve().parent
MODEL = os.environ.get("BUYLINE_AGENT_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # on a policy decline the API reruns the request on a fallback model


def _key() -> str:
    k = os.environ.get("ANTHROPIC_API_KEY")
    envf = HERE.parent / ".env"
    if not k and envf.exists():
        m = re.search(r"^ANTHROPIC_API_KEY=\"?([^\"\n]+)", envf.read_text(), re.M)
        k = m and m.group(1).strip()
    return k or ""


def _client(timeout: float = 120.0):
    key = _key()
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=1)


def _create(client, **kw):
    return client.beta.messages.create(model=MODEL, betas=[FALLBACK_BETA], fallbacks="default", **kw)


def _text(r) -> str:
    return "".join(b.text for b in r.content if b.type == "text").strip()


def _last_json(text: str, key: str) -> dict | None:
    dec, out = json.JSONDecoder(), None
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
            if isinstance(obj, dict) and key in obj:
                out = obj
        except ValueError:
            pass
    return out


# ---------------- discovery ----------------
def discover(spec: dict, n_max: int = 12) -> tuple[list[dict], dict, str]:
    """Claude + web search -> (candidates, sources {url: title}, error). Never raises."""
    try:
        client = _client(150.0)
        if not client:
            return [], {}, "no ANTHROPIC_API_KEY"
        item = spec.get("what") or spec.get("item")
        prompt = (
            "You are the sourcing researcher for Buyline, a procurement agent for small businesses.\n"
            f"Find real businesses that could sell us {spec.get('quantity')} {spec.get('unit')} of {item}"
            + (f", delivered to {spec['ship_to']}" if spec.get("ship_to") else "")
            + (f" within {spec['lead_days']} days" if spec.get("lead_days") else "") + ".\n"
            "Search the web. Prefer distributors and wholesalers that stock this exact item and serve the destination; "
            "include a few alternatives (retail, online, manufacturer, overseas) only if they could plausibly fill the "
            f"order, so the buyer sees the trade-offs. List {max(3, n_max - 4)} to {n_max} suppliers, each a different "
            "business.\nOur agent contacts suppliers by phone or email, so for each one look up the nearest branch's "
            "phone number or sales email on its own website or listing. Still list good suppliers whose contact you "
            "could not confirm (with null), but put the ones with confirmed contact first.\n"
            "Use only facts you found. Leave a field null when you did not find it. Never invent a phone number, email, "
            "price or address.\n"
            "Finish your reply with a single JSON object and nothing after it:\n"
            '{"suppliers": [{"name": "...", "type": "distributor|wholesaler|manufacturer|retail|online", '
            '"city": "... or null", "region": "state or country, or null", "distance_miles": <number or null>, '
            '"phone": "... or null", "email": "... or null", "website": "... or null", '
            '"stocks_item": "high|medium|low", "notes": "<one short line: minimum order, lead time or pricing hints>"}]}')
        messages = [{"role": "user", "content": prompt}]
        sources, text, deadline = {}, "", time.monotonic() + 150
        for _ in range(4):  # pause_turn: resend with the partial turn so the server resumes
            r = _create(client, max_tokens=16000, output_config={"effort": "medium"}, messages=messages,
                        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}])
            for b in r.content:
                if b.type == "web_search_tool_result" and isinstance(b.content, list):
                    for s in b.content:
                        sources.setdefault(s.url, s.title)
                elif b.type == "text":
                    text += b.text
            if r.stop_reason != "pause_turn" or time.monotonic() > deadline:
                break
            messages = [messages[0], {"role": "assistant", "content": r.content}]
        if r.stop_reason == "refusal":
            return [], sources, "the model declined the search"
        j = _last_json(text, "suppliers")
        if not j or not isinstance(j.get("suppliers"), list):
            return [], sources, f"no supplier list in the reply (stop_reason {r.stop_reason})"
        seen, out = set(), []
        for c in j["suppliers"]:
            name = str(c.get("name") or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(c)
        return out[:n_max], sources, ""
    except Exception as e:  # network, auth, parsing: the run falls back to the supplier book
        return [], {}, f"{type(e).__name__}: {str(e)[:160]}"


def score(c: dict, spec: dict) -> tuple[int, str | None]:
    """0-100 fit score, and the reason to skip it (None = eligible). Pure code: stock, type, reachability, distance."""
    stock = {"high": 40, "medium": 25, "low": 8}.get(str(c.get("stocks_item")).lower(), 15)
    kind = str(c.get("type") or "").lower()
    typ = {"distributor": 25, "wholesaler": 25, "manufacturer": 18, "online": 10, "retail": 8}.get(kind, 12)
    reach = (15 if c.get("phone") else 0) + (5 if c.get("email") else 0)
    d = c.get("distance_miles")
    far = isinstance(d, (int, float))
    dist = 15 if far and d <= 50 else 10 if far and d <= 300 else 4 if far and d <= 3000 else 0 if far else 7
    s = min(100, stock + typ + reach + dist)
    lead = spec.get("lead_days")
    if not c.get("phone") and not c.get("email"):
        return s, "no phone or email listed, so Fo can't reach them"
    if far and d > 3000 and lead and lead <= 21:
        return s, f"ships from overseas; risky for a {lead}-day deadline"
    return s, None


def pick_count(spec: dict, eligible: int, requested, cap: int | None) -> tuple[int, str]:
    """How many suppliers to contact. 'auto' scales with order value; the user's number wins; a cap (demo) wins over both."""
    value = spec.get("budget")
    if requested not in (None, "", "auto"):
        n, why = int(requested), f"you asked for {int(requested)}"
    else:
        n = 3 if not value or value < 1000 else 4 if value < 10000 else 5 if value < 50000 else 6
        why = (f"auto: an order of about ${value:,.0f} needs {n} quotes for a reliable price spread" if value
               else f"auto: {n} quotes give a reliable price spread")
    if cap and n > cap:
        n, why = cap, why + f", capped at {cap} for this run"
    if eligible < n:
        n, why = eligible, why + f"; only {eligible} candidates qualified"
    return max(n, 0), why


def source(spec: dict, mode: str = "ai", count="auto", book: list[dict] | None = None,
           cap: int | None = None) -> dict:
    """mode: ai (discover), mine (supplier book only), both. Returns state.json's `sourcing` block; shortlisted
    suppliers get ids A, B, C... in order (book first), the rest S1, S2..."""
    book = [dict(b, source=b.get("source") or "yours") for b in (book or [])]
    found, sources, err = ([], {}, "") if mode == "mine" else discover(spec)
    cands = []
    for b in book if mode in ("mine", "both") else []:
        cands.append({**b, "score": None, "skip": None, "why": "From your supplier book"})
    for c in found:
        s, skip = score(c, spec)
        loc = ", ".join(x for x in (c.get("city"), c.get("region")) if x)
        dist = c.get("distance_miles")
        cands.append({"name": c["name"], "source": "ai", "type": c.get("type"), "location": loc or None,
                      "distance": f"{dist:,.0f} mi" if isinstance(dist, (int, float)) else None,
                      "stock": c.get("notes") or None, "website": c.get("website"),
                      "listed_phone": c.get("phone"), "listed_email": c.get("email"),
                      "channels": [x for x in ("phone", "email") if c.get(x)], "score": s, "skip": skip,
                      "why": skip or f"{(c.get('type') or 'supplier').capitalize()}, fit score {s}"
                      + (", unclear they stock this exact item" if str(c.get("stocks_item")).lower() == "low" else "")})
    if mode != "mine" and not found and book:  # discovery failed: fall back to the book so the run still works
        cands = [{**b, "score": None, "skip": None, "why": "From your supplier book (search unavailable)"} for b in book]
    eligible = [c for c in cands if not c["skip"]]
    yours = [c for c in eligible if c["source"] != "ai"]
    new = sorted((c for c in eligible if c["source"] == "ai"), key=lambda c: -c["score"])
    n, why = pick_count(spec, len(eligible), count, cap)
    if mode == "both" and yours and new:  # keep incumbents honest: about half the slots go to new suppliers
        k = min(len(yours), max(1, n - max(1, n // 2)))
        short = yours[:k] + new[:n - k]
        short += [c for c in yours[k:] + new[n - k:]][:n - len(short)]
    else:
        short = (yours + new)[:n]
    letters, k = iter(string.ascii_uppercase), 0
    for c in cands:
        if c in short:
            c["id"], c["status"] = next(letters), "shortlisted"
            c["why"] = c["why"] if c["source"] != "ai" else f"Picked: {c['why'].lower()}"
        else:
            k += 1
            c["id"], c["status"] = f"S{k}", "skipped"
            if not c["skip"]:
                c["why"] = f"Ranked below the top {n} (fit score {c['score']})"
        c.pop("skip", None)
    short = sorted(short, key=lambda c: c["id"])
    return {"mode": mode, "target": count, "count": n, "count_why": why, "found": cands,
            "shortlist": [c["id"] for c in short], "sources": [{"url": u, "title": t} for u, t in list(sources.items())[:6]],
            "error": err or None}


# ---------------- strategy ----------------
def choose_round1(targets: list[dict], levers: list[dict]) -> dict | None:
    """Claude picks who to push first and with which real lever, from options code built.
    targets: [{id, name, unit_price, total, min_order, lead_days}]; levers: [{id, name, unit_price, terms}].
    Returns {target_id, lever_id, why} or None (code's rule applies). Validated: lever must undercut the target."""
    client = _client(60.0)
    if not client or not targets or not levers:
        return None
    schema = {"type": "object", "additionalProperties": False, "required": ["target_id", "lever_id", "why"],
              "properties": {"target_id": {"type": "string", "enum": [t["id"] for t in targets]},
                             "lever_id": {"type": "string", "enum": [x["id"] for x in levers]},
                             "why": {"type": "string"}}}
    try:
        r = _create(client, max_tokens=4000, output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                    system="You are Buyline's procurement agent planning a price negotiation for a small business. "
                           "Pick the one supplier worth pushing first (usually one we would actually buy from) and the "
                           "one honest lever to quote them. The lever must be cheaper per unit than the target's price. "
                           "In 'why', one plain sentence; use only numbers from the options.",
                    messages=[{"role": "user", "content": json.dumps({"targets": targets, "levers": levers})}])
        if r.stop_reason == "refusal":
            return None
        j = json.loads(_text(r))
        t = next(x for x in targets if x["id"] == j["target_id"])
        lv = next(x for x in levers if x["id"] == j["lever_id"])
        if lv["unit_price"] >= t["unit_price"] or lv["id"] == t["id"]:
            return None
        return j
    except Exception as e:
        print("agent.choose_round1:", repr(e)[:160], file=sys.stderr)
        return None


# ---------------- reasoning text ----------------
THINK_SYSTEM = ("You are Buyline's procurement agent, writing your reasoning into the buyer's live timeline. "
                "Write 2 to 4 short sentences of plain English in the first person plural ('we'). Explain why, not just "
                "what. Use only the names and numbers in the facts; never invent a price, supplier or term. No headings, "
                "bullets or markdown.")


def _money_ok(text: str, facts) -> bool:
    """Every dollar figure in the text must be a number in the facts, or a sum, difference or product of two of them
    (savings, totals, price x quantity). Anything else is treated as invented."""
    nums = sorted({float(x) for x in re.findall(r"\d+(?:\.\d+)?", json.dumps(facts))})[:200]
    allowed = {round(a, 2) for a in nums}
    for i, a in enumerate(nums):
        for b in nums[i:]:
            allowed.update({round(a + b, 2), round(abs(a - b), 2), round(a * b, 2)})
    return all(round(float(m.replace(',', '')), 2) in allowed for m in re.findall(r"\$\s?(\d[\d,]*(?:\.\d+)?)", text))


def think(task: str, facts: dict, fallback: str, secret: float | None = None) -> str:
    """One short reasoning paragraph from Claude, checked against the facts; the code-written fallback otherwise."""
    client = _client(45.0)
    if not client:
        return fallback
    try:
        r = _create(client, max_tokens=2000, output_config={"effort": "low"}, system=THINK_SYSTEM,
                    messages=[{"role": "user", "content": f"{task}\n\nFacts (the only names and numbers you may use):\n"
                                                           f"{json.dumps(facts, ensure_ascii=False)}"}])
        text = _text(r) if r.stop_reason != "refusal" else ""
    except Exception as e:
        print("agent.think:", repr(e)[:160], file=sys.stderr)
        return fallback
    if not text or not _money_ok(text, facts) or (secret and f"{secret:.2f}" in text):
        return fallback
    return text


if __name__ == "__main__":
    spec = {"item": "12AWG copper wire", "what": "12AWG THHN copper wire", "quantity": 500, "unit": "meters",
            "lead_days": 14, "budget": 500, "ship_to": "Berkeley CA"}
    t = time.monotonic()
    s = source(spec, "both", "auto", [{"name": "Supplier C", "phone": "+1 555 010 0003"}], cap=None)
    print(f"source: {time.monotonic() - t:.0f}s, error={s['error']}, count={s['count']} ({s['count_why']})")
    for c in s["found"]:
        print(f"  {c['id']:>3} {c['status']:<11} {str(c['score']):>4}  {c['name']}  [{c.get('location')}]  {c['why']}")
    facts = {"kestrel": {"unit_price": 0.80, "min_order": 1000, "total": 800}, "summit": {"total": 440}, "budget": 500}
    print("think:", think("Explain which quote we rule out and why.", facts, "fallback"))
    ok = not s["error"] and s["count"] >= 1 and len(s["shortlist"]) == s["count"]
    print("ok" if ok else "FAIL")
    sys.exit(0 if ok else 1)
