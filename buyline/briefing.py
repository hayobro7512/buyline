"""Morning briefing: one paragraph on what Buyline did overnight, spoken by Deepgram's Orpheus voice when
DEEPGRAM_API_KEY is set (env or ../.env), otherwise text only. Stdlib only. Fo's own call voice is never touched."""
import json, os, pathlib, re, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
VOICE = "aura-2-orpheus-en"


def deepgram_key() -> str:
    k = os.environ.get("DEEPGRAM_API_KEY")
    envf = HERE.parent / ".env"
    if not k and envf.exists():
        m = re.search(r"^DEEPGRAM_API_KEY=\"?([^\"\n]+)", envf.read_text(), re.M)
        k = m and m.group(1).strip()
    return k or ""


def script(st: dict) -> str:
    """'Overnight I called 3 suppliers, best landed price X, saved Y, PO waiting for your approval.'"""
    spec, rec = st.get("spec") or {}, st.get("recommendation") or {}
    quotes = [q for q in st.get("quotes") or [] if q.get("total") is not None]
    calls = [c for c in st.get("calls") or [] if c["kind"] == "quote"]
    best = next((q for q in quotes if q["id"] == rec.get("supplier")), None)
    if not best:
        return f"Good morning. Overnight I called {len(calls)} suppliers about {spec.get('what', 'your order')}, but no quote fit every constraint. Have a look at the dashboard."
    saved = max((q["total"] for q in quotes), default=best["total"]) - best["total"]
    negotiated = f" I talked them down from ${best['first_price']:.2f} to ${best['unit_price']:.2f} a {spec.get('unit', 'units').rstrip('s')}." if best.get("first_price") else ""
    return (f"Good morning. Overnight I called {len(calls)} suppliers about {spec.get('quantity')} {spec.get('unit')} of "
            f"{spec.get('what')}. Best landed price is {best['name']} at ${best['total']:.2f}, delivered in {best.get('lead_days')} days."
            f"{negotiated}" + (f" That saves ${saved:.2f} against the costliest quote." if saved > 0 else "")
            + (" The purchase order is waiting for your approval." if st.get("stage") != "ordered" else " The purchase order went out."))


SPEED = "1.1"  # the human's tuned live setting for this voice


def speak(text: str, out: pathlib.Path = HERE / "briefing.mp3") -> str | None:
    """Orpheus mp3 at out; None when there is no key or Deepgram fails (the dashboard then shows the text)."""
    key = deepgram_key()
    if not key:
        return None
    req = urllib.request.Request(f"https://api.deepgram.com/v1/speak?model={VOICE}&encoding=mp3&speed={SPEED}",
                                 json.dumps({"text": text}).encode(),
                                 {"Authorization": f"Token {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            audio = r.read()
        if not audio or "audio" not in r.headers.get("content-type", ""):
            return None
        out.write_bytes(audio)
        return out.name
    except Exception as e:
        print("deepgram:", repr(e)[:120])
        return None


def brief(st: dict, out: pathlib.Path = HERE / "briefing.mp3") -> dict:
    text = script(st)
    return {"text": text, "audio": speak(text, out), "voice": VOICE}


if __name__ == "__main__":  # self-check on the stub pipeline's state, then regenerate from state.json if present
    demo = {"stage": "approve", "spec": {"what": "12AWG copper wire", "quantity": 500, "unit": "meters"},
            "calls": [{"kind": "quote"}] * 3, "recommendation": {"supplier": "C"},
            "quotes": [{"id": "A", "name": "Supplier A", "total": 475.0, "unit_price": 0.95, "lead_days": 10},
                       {"id": "B", "name": "Supplier B", "total": 800.0, "unit_price": 0.80, "lead_days": 5},
                       {"id": "C", "name": "Supplier C", "total": 390.0, "unit_price": 0.78, "first_price": 0.88, "lead_days": 7}]}
    t = script(demo)
    assert "called 3 suppliers" in t and "Supplier C at $390.00" in t and "$0.88 to $0.78" in t and "$410.00" in t and "approval" in t, t
    print("check ok:", t)
    sf = HERE / "state.json"
    if sf.exists():
        st = json.loads(sf.read_text())
        st["briefing"] = brief(st)
        sf.write_text(json.dumps(st, indent=1))
        print("state.json briefing:", st["briefing"]["audio"] or "text only (no DEEPGRAM_API_KEY)")
