"""Cognee supplier memory: every quote and deal is remembered; the next order recalls them before Fo calls anyone.
Local open-source Cognee, own data dir under buyline/data. Heavy import (~40 s cold), so import this lazily."""
import asyncio, os, pathlib, re

HERE = pathlib.Path(__file__).resolve().parent
DATASET = "suppliers"


def _setup():
    envf = HERE.parent / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
    os.environ.update(LLM_PROVIDER="anthropic", LLM_MODEL=os.environ.get("LLM_MODEL", "claude-haiku-5-5"),
                      LLM_API_KEY=os.environ.get("ANTHROPIC_API_KEY", ""), EMBEDDING_PROVIDER="fastembed",
                      EMBEDDING_MODEL="sentence-transformers/all-MiniLM-L6-v2", EMBEDDING_DIMENSIONS="384",
                      EMBEDDING_MAX_TOKENS="256")
    import cognee
    cognee.config.data_root_directory(str(HERE / "data" / "cognee_data"))
    cognee.config.system_root_directory(str(HERE / "data" / "cognee_system"))
    return cognee


def remember(lines: list[str]) -> str:
    async def go():
        cognee = _setup()
        await cognee.add(lines, dataset_name=DATASET)
        await cognee.cognify(datasets=[DATASET])
    try:
        asyncio.run(go())
        return f"saved {len(lines)} supplier facts to Cognee"
    except Exception as e:
        return f"cognee error: {repr(e)[:120]}"


def recall(query: str, top_k: int = 8) -> list[str]:
    """Past supplier facts most related to this order, no LLM (CHUNKS)."""
    async def go():
        cognee = _setup()
        from cognee import SearchType
        res = await cognee.search(query_text=query, query_type=SearchType.CHUNKS, datasets=[DATASET], top_k=top_k)
        out = []
        for x in res:
            for r in (x.get("search_result", []) if isinstance(x, dict) and "search_result" in x else [x]):
                t = r.get("text") if isinstance(r, dict) else str(r)
                if t:
                    out.extend(s.strip() for s in re.split(r"(?<=\.)\s+(?=On \d{4})", t) if s.strip())
        return list(dict.fromkeys(out))
    try:
        return asyncio.run(go())
    except Exception as e:
        return [f"cognee error: {repr(e)[:120]}"]


if __name__ == "__main__":
    import sys
    print("\n".join(recall(" ".join(sys.argv[1:]) or "past supplier quotes")))
