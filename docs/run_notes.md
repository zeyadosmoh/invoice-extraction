# Extraction run notes (gemini-3.5-flash-lite)

Last updated: 2026-11-08T03:57:42+00:00

- `data/synthetic/pdf`: 120/120 extracted and cached (complete)
- `data/synthetic/scans`: 36/36 extracted and cached (complete)

Results: `data/extracted/gemini-3.5-flash-lite/<doc_id>.<ext>.json`; per-document log: `runs.csv` in the same folder.

All documents extracted. Re-running the commands below makes no API calls (everything is cached):

```bash
LLM_MODEL=gemini-3.5-flash-lite .venv/bin/python src/run_extraction.py --input data/synthetic/pdf && LLM_MODEL=gemini-3.5-flash-lite .venv/bin/python src/run_extraction.py --input data/synthetic/scans
```
