"""Fo adapters. Fo is a chat agent reached over MCP (ask_fo / get_fo_reply / get_fo_conversation), signed in inside
Claude Code; the live adapter runs a headless `claude -p` session that may use only those tools.
Modes: stub (canned replies), dry (builds the exact message, sends nothing), live (real call to FO_TEST_PHONE only)."""
import json, os, pathlib, re, subprocess, time

HERE = pathlib.Path(__file__).resolve().parent
TOOLS = "mcp__Fo__ask_fo,mcp__Fo__get_fo_reply,mcp__Fo__get_fo_conversation"
POLLS = 12  # x 40 s: a call can take a few minutes
CALL_WAIT = 10 * 60  # s to wait for the call's outcome after Fo says it's dialing
PENDING = re.compile(r"going out|dialing|calling now|on the (call|line)|once it wraps|I'll (bring|report|let you know)|"
                     r"will (report|bring)|when (it|the call) (wraps|ends|finishes)|as soon as it wraps|go(ing|ne) back", re.I)
HANGUP = " Once the price is agreed or refused, say goodbye once and hang up right away."
SEP = "\n---\n"  # between Fo's reports when one conversation held several calls
NO_ANSWER = re.compile(r"no answer|not answer|no one (picked|answered|spoke)|hung up|voicemail|line went dead|disconnected|"
                       r"didn't (speak|say|respond)|never reported|did not finish|call failed|could not read", re.I)
BAD_POLLS = 4  # unreadable outcome polls (API errors) before the call counts as no answer
REPLIES_STATUS = "Called the order desk. The order shipped this morning and should be delivered in 3 days."


def _env(name: str) -> str | None:
    p = os.environ.get(name)
    envf = HERE.parent / ".env"
    if not p and envf.exists():
        m = re.search(rf"^{name}=\"?([^\"\n]+)", envf.read_text(), re.M)
        p = m and m.group(1).strip()
    return p or None


def test_phone(supplier_id: str = "") -> str:
    """FO_TEST_PHONE_<id> if set (one teammate per supplier), else FO_TEST_PHONE. Dry and live dial nothing else."""
    p = (supplier_id and _env(f"FO_TEST_PHONE_{supplier_id}")) or _env("FO_TEST_PHONE")
    if not p:
        raise SystemExit("FO_TEST_PHONE is not set in .env; dry and live runs only ever dial that number")
    return p


def test_email() -> str:
    """Dry and live send the PO only to FO_TEST_EMAIL, never to a supplier and never by text."""
    e = _env("FO_TEST_EMAIL")
    if not e:
        raise SystemExit("FO_TEST_EMAIL is not set in .env; dry and live runs only ever email that address")
    return e


class StubFo:
    """Canned call transcripts. Never calls Fo."""
    mode = "stub"

    def __init__(self, replies: dict):
        self.replies = replies

    def ask(self, supplier: dict, message: str, on_status=lambda s: None) -> str:
        on_status("dialing")
        time.sleep(1.5)
        on_status("talking")
        time.sleep(2.5)
        if "status of purchase order" in message:
            return self.replies.get("status", REPLIES_STATUS)
        if " back " in message:
            n = self.replies["negotiate"]  # one string, or a dict by supplier id with "*" for the rest
            return n.get(supplier["id"], n.get("*", "")) if isinstance(n, dict) else n
        return self.replies["quotes"][supplier["id"]]

    def email(self, to: str, subject: str, body: str) -> str:
        return f"stub: PO email to {to} queued, not sent ({len(body)} chars)"


class SampleFo(StubFo):
    """The public link: canned call reports written for the item (orchestrate.sample_replies). No phone, ever."""
    mode = "sample"


class DryFo(StubFo):
    """Shows the exact Fo message (to FO_TEST_PHONE) and answers with the canned reply. Sends nothing."""
    mode = "dry"

    def ask(self, supplier, message, on_status=lambda s: None):
        return "DRY RUN, nothing sent. Canned reply: " + super().ask(supplier, message, on_status)

    def email(self, to, subject, body):
        return f"DRY RUN: would ask Fo to email the PO to {to}; nothing sent"


class LiveFo:
    """Real Fo through `claude -p`. Every call goes to FO_TEST_PHONE (a teammate playing the supplier)."""
    mode = "live"

    def __init__(self, log=HERE / "fo_log.jsonl"):
        self.phone = test_phone()
        self.phones = {test_phone(i) for i in "ABC"} | {self.phone}
        self.email_to = test_email()
        self.log = log

    def _claude(self, prompt: str, model: str = "claude-sonnet-5-5") -> str:
        assert any(p in prompt for p in self.phones) or self.email_to in prompt, "live Fo messages must name a test phone or FO_TEST_EMAIL"
        t0 = time.time()
        r = subprocess.run(["claude", "-p", prompt, "--model", model, "--output-format", "text",
                            "--allowedTools", TOOLS], capture_output=True, text=True, timeout=60 * 12,
                           cwd=HERE.parent)  # the Fo MCP is registered for the repo root, not buyline/
        out = (r.stdout or r.stderr).strip()
        if out.startswith("API Error") and model != "claude-opus-5-5":  # a safeguard flagged a routine poll: retry once elsewhere
            return self._claude(prompt, "claude-opus-5-5")
        with self.log.open("a") as fh:
            fh.write(json.dumps({"at": time.strftime("%H:%M:%S"), "secs": round(time.time() - t0), "prompt": prompt,
                                 "out": out}) + "\n")
        return out

    def ask(self, supplier: dict, message: str, on_status=lambda s: None) -> str:
        """One Fo conversation per call: start it, then poll get_fo_reply so the dashboard sees dialing -> talking."""
        on_status("dialing")
        cid = self._claude(
            "You are the Buyline procurement bridge. Call the tool ask_fo once with newConversation=true and this "
            f"exact message, changing nothing:\n\n{message}\n\nReply with ONLY the conversationId it returns, "
            "nothing else.").split()[-1].strip("`'\".")
        last = ""
        for _ in range(POLLS):
            out = self._claude(
                f"Call the tool get_fo_reply with conversationId={cid} and waitSeconds=40. "
                "Reply with ONLY a JSON object {\"status\": \"complete|working|failed\", \"text\": \"Fo's reply "
                f"text word for word\"}}, nothing else. (Reference, so the id is in this prompt: {self.phone})")
            m = re.search(r"\{.*\}", out, re.S)
            try:
                j = json.loads(m.group(0)) if m else {}
            except json.JSONDecodeError:
                j = {}
            last = j.get("text") or out
            if j.get("status") == "complete":
                return self._outcome(cid, last, on_status) if PENDING.search(last) else last
            if j.get("status") == "failed":
                return "Fo call failed: " + last
            on_status("talking")
        return "Fo did not finish in time. Last: " + last

    def _outcome(self, cid: str, ack: str, on_status) -> str:
        """Fo answers 'complete' with a 'call's going out' note before the call ends, and may call back a second time.
        Keep reading the conversation until the newest Fo message reports an outcome and is not followed by another
        call; the result is every report after the ack joined by SEP, the last one being the outcome."""
        on_status("on the call")
        t0, seen, candidate, bad = time.time(), [], None, 0
        while time.time() - t0 < CALL_WAIT:
            time.sleep(30)
            out = self._claude(
                f"Call the tool get_fo_conversation with conversationId={cid}. Reply with ONLY a JSON object "
                "{\"after\": [\"every assistant message newer than the one saying the call is going out, word for "
                "word, oldest first\"]}, nothing else. "
                f"(Reference, so the id is in this prompt: {self.phone})")
            m = re.search(r"\{.*\}", out, re.S)
            try:
                after = (json.loads(m.group(0)) if m else {}).get("after") or []
            except json.JSONDecodeError:
                after = []
            bad = 0 if m else bad + 1
            if bad >= BAD_POLLS:  # the poll itself keeps failing: fail visibly instead of waiting out CALL_WAIT
                return "Fo call failed: could not read the call's outcome: " + out[:200]
            seen = [x for x in after if isinstance(x, str) and x.strip() and x.strip() != ack.strip()] or seen
            last = seen[-1] if seen else ""
            if last and not PENDING.search(last):
                if last == candidate or last.rstrip().endswith("?"):  # stable, or Fo is asking us: the call is over
                    return SEP.join(seen)
                candidate = last  # non-pending report seen once; confirm it is still the newest on the next poll
        return "Fo never reported the call's outcome: " + (SEP.join(seen) or ack)

    def email(self, to: str, subject: str, body: str) -> str:
        assert to == self.email_to, "live PO emails go only to FO_TEST_EMAIL"
        return self._claude(
            "You are the Buyline procurement bridge. Call the tool ask_fo once with newConversation=true and this exact "
            f"message:\n\nSend an email to {to} with the subject '{subject}' and this body, word for word:\n{body}\n"
            "Email only; do not text or call anyone.\n\n"
            "Then call get_fo_reply with that conversationId and waitSeconds=40 until the status is complete or "
            "failed. Reply with only Fo's final reply text.")


def make(mode: str, replies: dict):
    return {"stub": lambda: StubFo(replies), "sample": lambda: SampleFo(replies), "dry": lambda: DryFo(replies), "live": LiveFo}[mode]()
