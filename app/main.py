"""Night Shift backend: night footage -> Haiku captions -> local Cognee (private watcher memory)
-> morning brief -> Cognee Cloud (shared family memory)."""
import asyncio, base64, io, json, os, pathlib, re, subprocess, time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAPS = DATA / "snaps"
SNAPS.mkdir(parents=True, exist_ok=True)
NIGHTS = ROOT / "nights"  # nights/<name>/ holds JPEGs/PNGs or one video file (kept out of git)
NIGHTS.mkdir(exist_ok=True)
LOG = DATA / "timeline.jsonl"
NORMS = DATA / "norms.json"  # alerts the caregiver marked normal; replays and live frames learn from them

envf = ROOT / ".env"
if envf.exists():
    for line in envf.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip().removeprefix("export "), v.strip().strip('"'))
KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("VISION_MODEL", "claude-haiku-5-5")
os.environ.update(
    LLM_PROVIDER="anthropic", LLM_MODEL=os.environ.get("LLM_MODEL", MODEL), LLM_API_KEY=KEY,
    EMBEDDING_PROVIDER="fastembed", EMBEDDING_MODEL="sentence-transformers/all-MiniLM-L6-v2",
    EMBEDDING_DIMENSIONS="384", EMBEDDING_MAX_TOKENS="256",
)

import anthropic  # noqa: E402
import cognee  # noqa: E402
from cognee import SearchType  # noqa: E402
from cognee.api.v1.serve.cloud_client import CloudClient  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from PIL import Image  # noqa: E402
from pydantic import BaseModel  # noqa: E402

cognee.config.data_root_directory(str(DATA / "cognee_data"))
cognee.config.system_root_directory(str(DATA / "cognee_system"))
DATASET = "watcher"  # local, private: raw captions
SHARED = "family_shared"  # Cognee Cloud: morning briefs only
BATCH_SECS = 15  # ponytail: fixed-interval cognify batches; tune if it backs up
CONCURRENCY = 4  # parallel Haiku captions during replay
NIGHT_START, NIGHT_HOURS = 22, 8  # replay frames are spread over 10 PM - 6 AM story time
DAY_START = 6  # a stove on between 6 AM and 10 PM is cooking, not an alert

claude = anthropic.AsyncAnthropic(api_key=KEY) if KEY else None
# Direct client = no global serve(), so local Cognee and Cloud coexist in one process.
cloud = CloudClient(os.environ["COGNEE_BASE_URL"], os.environ["COGNEE_API_KEY"]) \
    if os.environ.get("COGNEE_BASE_URL") and os.environ.get("COGNEE_API_KEY") else None

timeline = [json.loads(l) for l in LOG.read_text().splitlines()] if LOG.exists() else []
pending: list[str] = []
state = {"cognified": len(timeline), "cognifying": False, "last_error": None}
replay = {"night": None, "total": 0, "done": 0, "running": False}
briefs: dict[str, dict] = {}
norms: list[dict] = json.loads(NORMS.read_text()) if NORMS.exists() else []
NORMS_DS = "norms"  # local Cognee dataset: what the caregiver confirmed is normal, improved via session feedback
improving = {"running": False, "last": None}  # last cognee.improve() run: stages and seconds
kick = asyncio.Event()  # set when a replay ends so the batch is indexed at once

CATEGORIES = "sleep, bed, bathroom, water, kitchen, meds, stove, door, fall, empty, other"
PROMPT = (
    "You are Night Shift, a night camera watching over an elderly person who lives alone, while the "
    "caregiver sleeps. Describe this frame. Reply with JSON only:\n"
    '{"event":"<what the person is doing, max 10 words>","category":"<one of: ' + CATEGORIES + '>",'
    '"on":"<what the person\'s body rests on: bed, couch, chair, floor, standing, or none if nobody>",'
    '"matters":false,"why":"<only if matters: max 12 words>",'
    '"objects":[{"name":"pill bottle","where":"on the nightstand, left of the lamp"}]}\n'
    "matters=true ONLY for these: (a) the person's body is on the floor itself (sitting, kneeling or lying "
    "on the floor), even if calm (category fall, a possible fall); a person on a couch, chair, bed or low "
    "seat is NOT on the floor; a person lying down with no mattress, bed frame or bedding under them "
    "(rug, carpet, tiles or boards visible beneath) is on the floor, even if they look asleep; (b) a stove burner, oven or kettle that is ON (flame, glowing "
    "element, steam, lit indicator) with nobody right beside it (category stove); (c) a door to the outside or an "
    "exit door that is open, ajar or propped open, even with nobody there, or the person leaving the home "
    "(category door); bedroom, bathroom and closet doors do not count; (d) visible distress or injury. "
    "matters=false for everything else: sleeping, sitting on a couch, chair or bed, walking, drinking, "
    "bathroom, taking pills (category meds), an appliance that is off or that the person is standing at "
    "and using. List findable objects (pills, glass, phone, glasses, cane, keys...) with where they are. "
    "Never invent anything not visible."
)


async def caption(jpeg_b64: str) -> dict:
    if not claude:  # stub so the path runs without a key
        return {"event": "stub (no API key)", "category": "other", "matters": False, "why": "", "objects": []}
    msg = await claude.messages.create(
        model=MODEL, max_tokens=700, thinking={"type": "disabled"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg_b64}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    text = next((b.text for b in msg.content if b.type == "text"), "")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        out = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        out = {}
    return {"event": out.get("event", text[:80]), "category": out.get("category", "other"),
            "matters": bool(out.get("matters")), "why": out.get("why", ""), "objects": out.get("objects", []),
            "on": out.get("on", "")}


async def cognify_loop():
    while True:
        try:
            await asyncio.wait_for(kick.wait(), BATCH_SECS)
        except asyncio.TimeoutError:
            pass
        kick.clear()
        if not pending:
            continue
        batch = pending[:]
        del pending[:len(batch)]
        state["cognifying"] = True
        try:
            await cognee.add(batch, dataset_name=DATASET)
            await cognee.cognify(datasets=[DATASET])
            state["cognified"] += len(batch)
            state["last_error"] = None
        except Exception as e:  # keep the loop alive; put the batch back
            pending[:0] = batch
            state["last_error"] = repr(e)[:200]
        finally:
            state["cognifying"] = False


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(cognify_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/snaps", StaticFiles(directory=SNAPS), name="snaps")


# --- scene-change skip (vision-notes): mean abs diff on 32x24 gray ---
DIFF, FORCE_SECS = 8.0, 60 * 30  # tune DIFF 5-12; force a frame every 30 story-minutes
live_kept = {"gray": None, "ts": None}


def gray(jpeg: bytes) -> list[int]:
    return list(Image.open(io.BytesIO(jpeg)).convert("L").resize((32, 24)).getdata())


def changed(g: list[int], now: datetime, kept: dict) -> bool:
    prev, pts = kept["gray"], kept["ts"]
    d = 999.0 if prev is None else sum(abs(a - b) for a, b in zip(g, prev)) / len(g)
    if d < DIFF and pts and (now - pts).total_seconds() < FORCE_SECS:
        return False
    kept.update(gray=g, ts=now)
    return True


def jpeg640(raw: bytes) -> bytes:
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((640, 640))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=65)
    return buf.getvalue()


async def cognee_normal(cap: dict, now: datetime, night: str) -> str | None:
    """Ask Cognee's norms memory (shaped by improve()) whether this alert is normal for her."""
    if not norms or improving["running"]:
        return None
    try:
        res = await cognee.search(
            query_text=f"Night camera alert at {now:%-I:%M %p}: {cap['category']}: {cap['event']}. Is this normal "
                       "for her according to what the caregiver confirmed? Reply NORMAL or ALERT first, then one reason.",
            query_type=SearchType.GRAPH_COMPLETION, datasets=[NORMS_DS], session_id=f"check_{night}")
        text = " ".join(str(p) for p in results(res)).strip()
        return text if text.upper().startswith("NORMAL") else None
    except Exception as e:
        state["last_error"] = f"norms check: {repr(e)[:150]}"
        return None


async def ingest(jpeg: bytes, now: datetime, night: str) -> dict:
    sid = f"snap_{now:%Y%m%d_%H%M%S_%f}"
    (SNAPS / f"{sid}.jpg").write_bytes(jpeg)
    cap = await caption(base64.b64encode(jpeg).decode())
    if cap.get("on") == "floor" and not cap["matters"]:  # body on the floor is always a possible fall
        cap.update(matters=True, category="fall", why=cap["why"] or "person on the floor, possible fall")
    if cap["category"] in ("stove", "kitchen") and DAY_START <= now.hour < NIGHT_START:
        cap["matters"] = False  # ponytail: awake hours = cooking is normal (8 AM kettle); per-person hours via norms
    if cap["matters"] and (why := await cognee_normal(cap, now, night)):
        cap.update(matters=False, learned=f"Cognee: {why[:140]}")
    objs = "; ".join(f"{o.get('name')}: {o.get('where')}" for o in cap["objects"])
    flag = f" FLAGGED: {cap['why']}." if cap["matters"] else (" Learned normal." if cap.get("learned") else "")
    entry = {
        "id": sid, "night": night, "ts": now.isoformat(timespec="seconds"), "time": now.strftime("%-I:%M %p"),
        **cap,
        "text": f"{now:%Y-%m-%d %H:%M} ({now:%-I:%M %p}) [{night}] {cap['category']}: {cap['event']}.{flag}"
                f"{' Objects: ' + objs + '.' if objs else ''} ({sid})",
    }
    timeline.append(entry)
    with LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    pending.append(entry["text"])
    return entry


class Frame(BaseModel):
    image: str  # data URL or raw base64 JPEG
    ts: str | None = None  # simulated capture time (ISO); default now
    night: str = "live"


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.post("/frame")
async def frame(f: Frame):
    jpeg = base64.b64decode(f.image.split(",", 1)[-1])
    now = datetime.fromisoformat(f.ts) if f.ts else datetime.now()
    if not changed(gray(jpeg), now, live_kept):
        return {"skipped": True}
    return await ingest(jpeg, now, f.night)


class Rec(BaseModel):
    image: str
    night: str


@app.post("/record")
def record(r: Rec):
    """Save a raw frame to nights/<night>/ for later replay (no captioning)."""
    if not re.fullmatch(r"[\w-]{1,40}", r.night):
        raise HTTPException(400, "Night name: letters, digits, - or _ only")
    d = NIGHTS / r.night
    d.mkdir(exist_ok=True)
    n = len(list(d.glob("*.jpg")))
    (d / f"f_{n:05d}.jpg").write_bytes(base64.b64decode(r.image.split(",", 1)[-1]))
    return {"frames": n + 1}


# --- replay a recorded night ---
def night_frames(night: str) -> list[pathlib.Path]:
    d = NIGHTS / night
    if not d.is_dir():
        raise HTTPException(404, f"No footage at nights/{night}/")
    imgs = sorted(f for f in d.iterdir() if f.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if imgs:
        return imgs
    vids = [f for f in d.iterdir() if f.suffix.lower() in {".mov", ".mp4", ".m4v", ".webm"}]
    if not vids:
        raise HTTPException(404, f"nights/{night}/ has no images or video")
    out = DATA / "frames" / night
    if not out.exists():  # one frame every 2 s of footage, extracted once
        out.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(vids[0]), "-vf", "fps=1/2",
                        str(out / "f_%05d.jpg")], check=True)
    return sorted(out.glob("*.jpg"))


async def run_replay(night: str, files: list[pathlib.Path]):
    try:
        day = datetime.now().replace(hour=NIGHT_START, minute=0, second=0, microsecond=0)
        step = timedelta(hours=NIGHT_HOURS) / max(len(files), 1)
        kept, last = [], {"gray": None, "ts": None}
        for i, f in enumerate(files):  # diff is sequential and cheap; captions run in parallel
            jpeg = jpeg640(f.read_bytes())
            ts = day + i * step
            if hm := re.search(r"_(\d{2})(\d{2})$", f.stem):  # 08_0340.jpg = shot at 3:40 AM
                ts = day.replace(hour=int(hm[1]), minute=int(hm[2])) + timedelta(days=int(hm[1]) < 12)
            if changed(gray(jpeg), ts, last):
                kept.append((jpeg, ts))
        replay.update(total=len(kept))
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(jpeg, ts):
            async with sem:
                await ingest(jpeg, ts, night)
                replay["done"] += 1

        await asyncio.gather(*(one(j, t) for j, t in kept))
        kick.set()
        await make_brief(night)
    except Exception as e:
        state["last_error"] = repr(e)[:200]
    finally:
        replay["running"] = False


class Replay(BaseModel):
    night: str = "night1"


@app.get("/nights")
def nights():
    return sorted(p.name for p in NIGHTS.iterdir() if p.is_dir())


@app.post("/replay")
async def start_replay(r: Replay):
    if replay["running"]:
        raise HTTPException(409, "A replay is already running")
    files = night_frames(r.night)
    timeline[:] = [e for e in timeline if e["night"] != r.night]  # fresh run of that night
    LOG.write_text("".join(json.dumps(e) + "\n" for e in timeline))
    briefs.pop(r.night, None)
    replay.update(night=r.night, total=len(files), done=0, running=True)
    asyncio.create_task(run_replay(r.night, files))
    return {"frames": len(files)}


# --- morning brief: written by Haiku from the night's events, shared via Cognee Cloud ---
def night_entries(night: str) -> list[dict]:
    return sorted((e for e in timeline if e["night"] == night), key=lambda e: e["ts"])


async def make_brief(night: str) -> dict:
    ev = night_entries(night)
    flagged = [e for e in ev if e["matters"]]
    lines = "\n".join(e["text"] for e in ev)
    summary = f"{len(ev)} events, {len(flagged)} flagged."
    if claude and ev:
        msg = await claude.messages.create(
            model=MODEL, max_tokens=400, thinking={"type": "disabled"},
            messages=[{"role": "user", "content":
                       "Write the caregiver's morning brief from these night camera events of an elderly "
                       "parent. 3 short sentences, plain words: how the night went, what matters (with "
                       f"times), anything to check. No photos mentioned.\n\n{lines}"}],
        )
        summary = next((b.text for b in msg.content if b.type == "text"), summary).strip()
    b = {"night": night, "events": len(ev), "flagged": len(flagged), "summary": summary,
         "learned": sum(bool(e.get("learned")) for e in ev),
         "flagged_events": [{k: e[k] for k in ("id", "time", "event", "why", "category")} for e in flagged],
         "cloud": "not configured"}
    briefs[night] = b
    if cloud:  # shared memory: summary text only, never photos
        try:
            date = ev[0]["ts"][:10] if ev else ""
            await cloud.add([f"Night Shift morning brief, {night} ({date}): {summary} "
                             f"Events: {len(ev)}, flagged: {len(flagged)}. Flagged: "
                             + "; ".join(f"{e['time']} {e['event']} ({e['why']})" for e in flagged)],
                            dataset_name=SHARED)
            await cloud.cognify(datasets=[SHARED], run_in_background=True)
            b["cloud"] = "shared to Cognee Cloud"
        except Exception as e:
            b["cloud"] = f"cloud error: {repr(e)[:120]}"
    return b


# --- roles: who sees what (caregiver everything; doctor health events, no photos; family the brief only) ---
ROLES = {"caregiver": None, "doctor": {"sleep", "bed", "bathroom", "water", "meds", "fall"}, "family": set()}


def check_role(role: str) -> str:
    # ponytail: role is picked on the page, not logged in; snaps stay reachable by URL. Add auth + Cognee
    # dataset permissions (ENABLE_BACKEND_ACCESS_CONTROL) before real families use it.
    if role not in ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(ROLES)}")
    return role


# --- learn loop: the caregiver marks an alert normal; later nights hold back alerts like it ---
class Normal(BaseModel):
    id: str


@app.post("/normal")
async def mark_normal(n: Normal):
    e = next((e for e in timeline if e["id"] == n.id), None)
    if not e:
        raise HTTPException(404, "No such event")
    ts = datetime.fromisoformat(e["ts"])
    norms.append({"category": e["category"], "minute": ts.hour * 60 + ts.minute, "time": e["time"], "event": e["event"]})
    NORMS.write_text(json.dumps(norms))
    if e["matters"]:
        e.update(matters=False, learned="you marked it normal")
        LOG.write_text("".join(json.dumps(x) + "\n" for x in timeline))
        if b := briefs.get(e["night"]):
            b["flagged_events"] = [f for f in b["flagged_events"] if f["id"] != e["id"]]
            b.update(flagged=len(b["flagged_events"]), learned=b.get("learned", 0) + 1)
    asyncio.create_task(teach_cognee(e))
    return {"norms": len(norms), "improving": True}


async def teach_cognee(e: dict):
    """Caregiver feedback through Cognee's own loop: norm text, session Q&A + feedback, then improve()."""
    improving["running"] = True
    t0 = time.perf_counter()
    note = f"Caregiver confirmed: {e['category']} around {e['time']} ({e['event']}) is normal for her, not an alert."
    try:
        await cognee.add([note], dataset_name=NORMS_DS)
        await cognee.cognify(datasets=[NORMS_DS])
        await cognee.search(query_text=f"Is this alert normal for her? {e['time']} {e['category']}: {e['event']}",
                            query_type=SearchType.GRAPH_COMPLETION, datasets=[NORMS_DS], session_id="caregiver")
        qa_id = (await cognee.session.get_session(session_id="caregiver"))[-1].qa_id
        await cognee.session.add_feedback("caregiver", qa_id, feedback_text="This is normal for her", feedback_score=1)
        # improve() bridges session Q&A text into the graph (feedback text alone isn't stored), so say it as a turn
        await cognee.search(query_text=f"Caregiver correction: {note} Do not alert for this.",
                            query_type=SearchType.GRAPH_COMPLETION, datasets=[NORMS_DS], session_id="caregiver")
        res = await cognee.improve(dataset=NORMS_DS, session_ids=["caregiver"])
        done = [st.stage for st in res.stages if st.status == "completed"]
        improving["last"] = {"stages": done, "secs": round(time.perf_counter() - t0, 1), "status": str(res.status)}
    except Exception as ex:
        improving["last"] = {"error": repr(ex)[:200]}
    finally:
        improving["running"] = False
    pending.append(note)  # watcher memory too, so ask can answer "what have you learned is normal for her?"


@app.delete("/norms")
def forget_norms():
    norms.clear()
    NORMS.unlink(missing_ok=True)
    # ponytail: Cognee's norms graph is kept; rm -rf data (server stopped) for a true clean slate
    return {"norms": 0}


@app.get("/brief")
async def brief(night: str = "night1", role: str = "caregiver"):
    role = check_role(role)
    if night not in briefs:
        if replay["running"] and replay["night"] == night:
            raise HTTPException(425, "Night still replaying")
        if not night_entries(night):
            raise HTTPException(404, "No events for that night yet")
        await make_brief(night)
    b = briefs[night]
    if role == "caregiver":
        return b
    see = ROLES[role]
    return {**b, "flagged_events": [{k: v for k, v in e.items() if k != "id"}
                                    for e in b["flagged_events"] if e["category"] in see]}


@app.get("/timeline")
def get_timeline(night: str | None = None, role: str = "caregiver"):
    role = check_role(role)
    ev = [e for e in timeline if night in (None, e["night"])]
    shown = ev if role == "caregiver" else [
        {k: v for k, v in e.items() if k not in ("id", "objects", "text")} for e in ev if e["category"] in ROLES[role]]
    ev.sort(key=lambda e: e["ts"], reverse=True)
    shown.sort(key=lambda e: e["ts"], reverse=True)
    return {"entries": shown[:80], "events": len(ev), "flagged": sum(e["matters"] for e in ev),
            "learned": sum(bool(e.get("learned")) for e in ev), "norms": len(norms), "improve": improving,
            "memories": len(timeline), "pending": len(pending), "replay": replay, **state}


# --- ask: Cognee graph answer + CHUNKS (no LLM) for the photo and newest-first history ---
STOP = {"where", "what", "when", "did", "the", "she", "her", "his", "him", "is", "are", "was", "last",
        "see", "saw", "have", "you", "and", "for", "take", "night", "does", "any"}


def words(s: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", s.lower()) if len(w) > 2 and w not in STOP}


def results(res) -> list:
    return [r for x in res for r in (x.get("search_result", []) if isinstance(x, dict) else [x])]


async def graph_answer(q: str) -> str | None:
    res = await cognee.search(
        query_text=f"{q}\nThe memories are timestamped observations from a night camera watching over an "
                   "elderly person. Answer in one or two sentences with the times. Say so if it never happened.",
        query_type=SearchType.GRAPH_COMPLETION, datasets=[DATASET],
    )
    return " ".join(str(p) for p in results(res)) or None


async def chunk_hits(q: str) -> list[dict]:
    """Timeline entries for Cognee's CHUNKS hits, most relevant first."""
    res = await cognee.search(query_text=q, query_type=SearchType.CHUNKS, datasets=[DATASET], top_k=10)
    by_id = {e["id"]: e for e in timeline}
    ids = re.findall(r"snap_\d{8}_\d{6}_\d+", " ".join(map(str, results(res))))
    return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]


async def family_answer(q: str) -> str:
    if cloud:
        try:
            res = await cloud.search(q, search_type="GRAPH_COMPLETION", datasets=[SHARED])
            if text := " ".join(str(p) for p in results(res)):
                return text
        except Exception as e:
            state["last_error"] = f"cloud search: {repr(e)[:150]}"
    return " ".join(b["summary"] for b in briefs.values()) or "No morning brief shared yet."


class Ask(BaseModel):
    q: str
    role: str = "caregiver"


@app.post("/ask")
async def ask(a: Ask):
    t0 = time.perf_counter()
    role = check_role(a.role)
    if role == "family":  # family reads only the shared Cloud memory (morning briefs), never the private watcher
        return {"answer": await family_answer(a.q), "snap": None, "entry": None, "history": [],
                "secs": round(time.perf_counter() - t0, 1)}
    ga, hits = await asyncio.gather(graph_answer(a.q), chunk_hits(a.q), return_exceptions=True)
    for r in (ga, hits):
        if isinstance(r, Exception):
            state["last_error"] = repr(r)[:200]
    answer = None if isinstance(ga, Exception) else ga
    hits = [] if isinstance(hits, Exception) else hits
    qw = words(a.q)
    # ponytail: keyword match over hits picks the photo; Haiku query parsing if this misses
    matched = [e for e in hits if qw & words(e["text"])]
    if not matched:  # Cognee not indexed yet (right after a replay): fall back to the raw timeline
        matched = sorted((e for e in timeline if qw & words(e["text"])), key=lambda e: e["ts"], reverse=True)
    hit = matched[0] if matched else None  # no photo beats a wrong photo
    history = sorted(matched, key=lambda e: e["ts"], reverse=True)
    if not answer and not hit:
        raise HTTPException(404, "No memory of that yet.")
    indexing = bool(pending) or state["cognifying"]
    if role != "caregiver":  # doctor: health events from the log only; the graph answer could mention anything
        history = [e for e in history if e["category"] in ROLES[role]]
        answer = None if history else "Nothing health-related on that last night."
    if (not answer or indexing) and history:  # the graph lags the log right after a replay: trust the log
        answer = "; ".join(f"{e['time']}: {e['event']}" for e in history[:3]) + (
            " (from tonight's log; memory still indexing)" if indexing else "")
    if role != "caregiver":  # no photos outside the caregiver; doctor keeps health history, family the answer only
        hit = None
        history = [{**e, "id": ""} for e in history]
    return {"answer": answer,
            "snap": hit and f"/snaps/{hit['id']}.jpg", "entry": hit,
            "history": [{k: e[k] for k in ("id", "time", "event")} for e in history[:5]],
            "secs": round(time.perf_counter() - t0, 1)}
