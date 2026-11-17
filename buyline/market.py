#!/usr/bin/env python3
"""Market price research before Fo calls anyone: one Claude call with server-side web search.
research(spec) -> {low, typical, high, unit, sources, note, soft_line, hard_line} or {"error": ...}. Never raises.
  self-check: python3 market.py"""
import json, re, sys, time

import orchestrate

MODEL = "claude-sonnet-5-5"


def _last_json(text: str) -> dict | None:
    dec, out = json.JSONDecoder(), None
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
            if isinstance(obj, dict) and "typical" in obj:
                out = obj
        except ValueError:
            pass
    return out


def research(spec: dict) -> dict:
    try:
        import anthropic
        key = orchestrate.api_key()
        if not key:
            return {"error": "no ANTHROPIC_API_KEY"}
        qty, unit, budget = spec.get("quantity") or 1, spec.get("unit") or "unit", spec.get("budget")
        item = " ".join(str(x) for x in [spec.get("item"), spec.get("size"), *(spec.get("features") or [])] if x)
        prompt = (
            f"Research the current US market price for: {item}. We are buying about {qty} {unit}"
            + (f", shipping to {spec['ship_to']}" if spec.get("ship_to") else "") + ".\n"
            "Search the web for real current prices from suppliers or retailers, at roughly this quantity. "
            f"Convert every price to USD per single {unit} (e.g. a $90 spool of 100 ft is $0.90/ft = $2.95/meter).\n"
            "Finish your reply with a single JSON object and nothing after it:\n"
            f'{{"low": <cheapest credible price per {unit}>, "typical": <typical price per {unit}>, '
            f'"high": <high end per {unit}>, "unit": "{unit}", "note": "<one plain sentence on what drives the range>"}}')
        client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=0)
        messages = [{"role": "user", "content": prompt}]
        deadline = time.monotonic() + 60
        sources, text = {}, ""
        for _ in range(3):  # pause_turn: resend with the partial turn appended so the server resumes
            r = client.messages.create(
                model=MODEL, max_tokens=8000, output_config={"effort": "medium"}, messages=messages,
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}])
            for b in r.content:
                if b.type == "web_search_tool_result" and isinstance(b.content, list):
                    for s in b.content:
                        sources.setdefault(s.url, s.title)
                elif b.type == "text":
                    text += b.text
                    for c in b.citations or []:
                        if getattr(c, "url", None):
                            sources.setdefault(c.url, c.title)
            if r.stop_reason != "pause_turn" or time.monotonic() > deadline:
                break
            messages = [messages[0], {"role": "assistant", "content": r.content}]
        if r.stop_reason == "refusal":
            return {"error": "model refused"}
        j = _last_json(text)
        if not j:
            return {"error": f"no JSON in reply (stop_reason {r.stop_reason})"}
        low, typical, high = (float(j[k]) for k in ("low", "typical", "high"))
        low, typical, high = sorted((low, typical, high))
        cap = budget / qty if budget else high
        return {"low": low, "typical": typical, "high": high, "unit": j.get("unit") or unit,
                "sources": [{"title": t, "url": u} for u, t in list(sources.items())[:4]],
                "note": str(j.get("note") or ""),
                "soft_line": round(typical * 0.95, 2), "hard_line": round(min(high, cap), 2)}
    except Exception as e:
        return {"error": repr(e)[:200]}


if __name__ == "__main__":
    bad = []
    for spec in [{"item": "12AWG copper wire", "quantity": 500, "unit": "meter", "budget": 425, "ship_to": "Berkeley CA"},
                 {"item": "#4 rebar (1/2 inch iron bar), 20 ft", "quantity": 200, "unit": "bar", "budget": 4000}]:
        t = time.monotonic()
        res = research(spec)
        print(f"{spec['item']}  ({time.monotonic() - t:.1f}s)\n{json.dumps(res, indent=2)}\n")
        for ok, why in [("error" not in res, "error"), (res.get("low", 0) <= res.get("typical", 0) <= res.get("high", 0), "low<=typical<=high"),
                        (res.get("soft_line", 1) < res.get("hard_line", 0), "soft_line < hard_line (budget below market?)"),
                        (bool(res.get("sources")), "sources")]:
            if not ok:
                bad.append(f"{spec['item']}: {why}")
    print("FAIL:\n  " + "\n  ".join(bad) if bad else "ok")
    sys.exit(1 if bad else 0)
