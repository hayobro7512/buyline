#!/bin/sh
# One command: ./run.sh  → http://localhost:8000
cd "$(dirname "$0")" && exec uv run --python 3.12 uvicorn app.main:app --port 8000
