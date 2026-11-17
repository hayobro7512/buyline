"""Buyline dashboard server (stdlib): serves this folder like http.server, plus the approve buttons.
POST /approve finishes the saved state.json approve -> ordered (PO sent, no Fo call); POST /not-now leaves it at approve.
POST /cancel {po?}: with a po, marks that PO cancelled in orders.json (not once delivered). Without one, stops the run /start
began (that pid only) and sets state.json stage 'cancelled', or, once ordered, cancels the state's PO. No Fo call, no email.
POST /start {request, image?, media_type?} turns an invoice image (png/jpeg/pdf, <=4 MB) into one request line via Claude, then runs demo_reset.sh, then orchestrate.py --from-prerun ... --until approve in the background (log: run.log).
Mode comes from env, never from the page. BUYLINE_MODE=sample python3 serve.py 8020 (public link: canned sample calls, no phone)
BUYLINE_MODE=live python3 serve.py 8030 (demo link: real Fo to the test phone, quotes from prerun.json). BUYLINE_FO=stub|dry|live|sample overrides
serve.py imports orchestrate in-process for /approve: restart it to pick up orchestrate.py changes.
Run from buyline/:  python3 serve.py            # http://localhost:8020/"""
import json, os, subprocess, sys, threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import orchestrate

PY = HERE.parent / ".venv" / "bin" / "python"
INVOICE_PROMPT = ("This is a purchasing document: an invoice, order, quote, parts list, bill of materials or spec sheet. "
                  "Transcribe it as one line per item: item, spec, quantity and unit, unit price if given, supplier if "
                  "given. Output only those lines, nothing else.")
MODE = os.environ.get("BUYLINE_MODE", "sample")  # sample: public link, canned calls for any item, no phone, no prerun. live: demo link, real Fo to the test phone
LOCK, RUN = threading.Lock(), None  # the run started by /start; ponytail: one run at a time, per server process


def read_invoice(b64: str, media_type: str) -> tuple[str, str]:
    """One Claude call: invoice image -> one request line. Returns (line, error)."""
    if media_type not in ("image/png", "image/jpeg", "application/pdf"):
        return "", f"unsupported file type {media_type!r}: use png, jpeg or pdf"
    if len(b64) > 4 * 1024 * 1024 * 4 // 3 + 4:
        return "", "image is over 4 MB"
    key = orchestrate.api_key()
    if not key:
        return "", "Claude: no ANTHROPIC_API_KEY in env or .env"
    try:
        import anthropic
        msg = anthropic.Anthropic(api_key=key, timeout=60.0).beta.messages.create(
            model="claude-sonnet-5-5", max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",  # retry on another model if a classifier declines
            messages=[{"role": "user", "content": [
                {"type": "document" if media_type == "application/pdf" else "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": INVOICE_PROMPT}]}])
    except Exception as e:  # any API/network failure: report it, don't start a run
        return "", f"Claude: invoice read failed: {type(e).__name__}: {getattr(e, 'message', e)}"
    if msg.stop_reason == "refusal":
        return "", "Claude: declined to read that image"
    line = " ".join(b.text for b in msg.content if b.type == "text").strip()
    return (line, "") if line else ("", "Claude: returned no text for the image")


def sourcing_args(body: dict) -> list[str]:
    """The New request form's supplier choices -> orchestrate.py flags. BUYLINE_SOURCING sets the default mode,
    BUYLINE_MAX_SUPPLIERS caps the count for a demo (orchestrate reads it from the env)."""
    mode = body.get("sourcing_mode") or os.environ.get("BUYLINE_SOURCING", "book")
    if mode not in ("book", "ai", "mine", "both"):
        mode = "book"
    count = str(body.get("target_count") or "auto")
    count = count if count == "auto" or count.isdigit() else "auto"
    mine = [{k: str(x.get(k) or "")[:120] for k in ("name", "phone", "email")} for x in (body.get("my_suppliers") or [])
            if isinstance(x, dict) and x.get("name")][:10]
    return ["--sourcing", mode, "--count", count] + (["--my-suppliers", json.dumps(mine)] if mine else [])


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/start":
            return self.start()
        if self.path == "/cancel":
            return self.cancel()
        if self.path not in ("/approve", "/not-now"):
            return self.send_error(404)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = {}
        res = orchestrate.approve(HERE / "state.json", yes=self.path == "/approve", supplier=body.get("supplier") or None)
        if self.path == "/approve":  # the PO as a PDF at /po/<number>.pdf; a failed render never blocks the order
            try:
                import po_pdf
                po_pdf.make(json.loads((HERE / "state.json").read_text()))
            except Exception as e:
                print("po_pdf:", e, file=sys.stderr)
        self.reply(200, res)

    def start(self):
        global RUN
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            req = str(body.get("request") or "").strip()
        except (ValueError, AttributeError):
            body, req = {}, ""
        if body.get("image"):
            line, err = read_invoice(body["image"], body.get("media_type", ""))
            if err:
                return self.reply(502 if err.startswith("Claude") else 400, {"ok": False, "error": err})
            req = (req + "\n\nFrom the attached document:\n" + line).strip()
        if not req:
            return self.reply(400, {"ok": False, "error": "request is empty"})
        with LOCK:  # ThreadingHTTPServer: two clicks must not start two runs
            if RUN and RUN.poll() is None:
                return self.reply(409, {"ok": False, "error": f"a run is still going (pid {RUN.pid})", "pid": RUN.pid})
            fo = os.environ.get("BUYLINE_FO") or {"sample": "sample", "live": "live"}.get(MODE, "stub")
            subprocess.run(["sh", str(HERE / "demo_reset.sh")], cwd=HERE, check=True)
            with open(HERE / "run.log", "w") as log:
                RUN = subprocess.Popen([str(PY), "orchestrate.py", req, *(["--from-prerun"] if os.environ.get("BUYLINE_PRERUN", "0" if MODE == "sample" else "1") != "0" else []), "--fo", fo, "--pause", os.environ.get("BUYLINE_PAUSE", "5"), "--rounds", str(body.get("rounds") or os.environ.get("BUYLINE_ROUNDS", "1")), "--until", "approve", *sourcing_args(body)],
                                       cwd=HERE, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        self.reply(200, {"ok": True, "pid": RUN.pid})

    def cancel(self):
        try:
            po = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}").get("po")
        except (ValueError, AttributeError):
            po = None
        path = HERE / "state.json"
        with LOCK:
            st = json.loads(path.read_text()) if path.exists() else {}
            if not po and st.get("stage") not in ("ordered", "cancelled"):  # mid-run: stop it, nothing was ordered
                if RUN and RUN.poll() is None:
                    RUN.terminate()
                    try:
                        RUN.wait(5)
                    except subprocess.TimeoutExpired:
                        RUN.kill()
                        RUN.wait()
                    st = json.loads(path.read_text()) if path.exists() else st  # keep what the run got to
                if not st:
                    return self.reply(409, {"ok": False, "error": "nothing to cancel"})
                st["stage"], st["decision"] = "cancelled", "cancelled"
                orchestrate.save_state(st, path)
                return self.reply(200, {"ok": True, "stage": "cancelled"})
            po = po or (st.get("po") or {}).get("number")
            o = next((o for o in orchestrate.load_orders() if o["po"] == po), None)
            if not o:
                return self.reply(404, {"ok": False, "error": f"no order {po}"})
            if o["status"] in ("delivered", "cancelled"):
                return self.reply(409, {"ok": False, "error": f"{po} is already {o['status']}"})
            orchestrate.set_status(po, "cancelled", "Cancelled from the dashboard")
            if (st.get("po") or {}).get("number") == po:
                st["po"]["status"] = st["decision"] = "cancelled"
                orchestrate.save_state(st, path)
        self.reply(200, {"ok": True, "po": po, "status": "cancelled"})

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # state.json and orders.json must never be stale
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
    print(f"Buyline dashboard on http://localhost:{port}/  (POST /start, /approve, /not-now, /cancel; Fo {os.environ.get('BUYLINE_FO', 'stub')})")
    ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(HERE))).serve_forever()
