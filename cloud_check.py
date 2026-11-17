"""Prove Cognee Cloud works from our stack: add 3 captions, cognify, search, time each.

uv run python cloud_check.py   (reads COGNEE_BASE_URL / COGNEE_API_KEY from .env; never touches the server)
"""
import asyncio, json, os, pathlib, tempfile, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip().removeprefix("export "), v.strip().strip('"'))

import cognee  # noqa: E402
from cognee import SearchType  # noqa: E402

# If any call silently runs locally, it lands here (not in data/), and we report it.
LOCAL = pathlib.Path(tempfile.mkdtemp(prefix="cloudcheck_"))
cognee.config.data_root_directory(str(LOCAL / "data"))
cognee.config.system_root_directory(str(LOCAL / "system"))

CAPTIONS = [
    "2026-11-16 23:05 (11:05 PM) camera saw: keys: on the desk, left of the laptop; mug: next to the lamp. (snap_a)",
    "2026-11-16 23:40 (11:40 PM) camera saw: keys: inside the black backpack on the chair; laptop: closed on the desk. (snap_b)",
    "2026-11-17 02:10 (2:10 AM) camera saw: cat: asleep on the couch; backpack: on the chair. (snap_c)",
]


DS = f"recall_cloud_check_{int(time.time())}"  # fresh dataset each run, so timings are honest


async def wait_built(dataset_id: str, timeout=300):
    url = f"{os.environ['COGNEE_BASE_URL']}/api/v1/datasets/status?dataset={dataset_id}"
    req = urllib.request.Request(url, headers={"X-Api-Key": os.environ["COGNEE_API_KEY"]})
    for _ in range(timeout):
        status = json.dumps(await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(req, timeout=30))))
        if "COMPLETED" in status:
            return
        if "ERRORED" in status:
            raise RuntimeError(f"cloud cognify failed: {status[:300]}")
        await asyncio.sleep(1)
    raise TimeoutError("cloud cognify still running")


async def main():
    t = time.perf_counter
    await cognee.serve(url=os.environ["COGNEE_BASE_URL"], api_key=os.environ["COGNEE_API_KEY"])
    try:
        t0 = t(); await cognee.add(CAPTIONS, dataset_name=DS); t1 = t()
        cog = await cognee.cognify(datasets=[DS])  # cloud returns PipelineRunStarted; poll until built
        await wait_built(next(iter(cog))); t2 = t()
        r = await cognee.search("Where are my keys and when were they last seen?",
                                query_type=SearchType.GRAPH_COMPLETION, datasets=[DS]); t3 = t()
    finally:
        await cognee.disconnect()
    print(f"cloud add {t1-t0:.1f}s, cognify {t2-t1:.1f}s, search {t3-t2:.1f}s, total {t3-t0:.1f}s")
    print("answer:", r)
    local = [p for p in LOCAL.rglob("*") if p.is_file()]
    print("ran locally instead?", f"YES, {len(local)} local files" if local else "no, all in cloud")


asyncio.run(main())
