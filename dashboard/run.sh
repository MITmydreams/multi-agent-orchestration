#!/bin/bash
cd "$(dirname "$0")/.."
.venv/bin/python -m uvicorn dashboard.backend:app --host 0.0.0.0 --port 8765 --reload
