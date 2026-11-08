# n8n workflow: process invoices dropped in a folder

`workflows/invoice_pipeline.json` polls `data/inbox/` every 30 seconds. Each PDF/PNG found is sent to the
local pipeline API (`src/api.py`), which extracts, validates against every invoice already in Postgres
and loads it. The API then moves the file to `data/processed/` (auto-approved) or `data/needs_review/`
(failed a rule, or could not be processed) and, for the latter, appends a row with the reasons to
`data/needs_review/review_log.csv`.

```
Every 30 seconds -> Read inbox files -> POST /process -> Auto-approved? -> POST /move (processed)
                                             |                       \-> POST /move (needs_review + log)
                                             \-> on error -> POST /move (needs_review, reason = the error)
```

The workflow file contains no secrets. The API token lives in an n8n credential named
**Invoice pipeline API** that you create once.

## Setup

1. Set `PIPELINE_API_TOKEN` in `.env` (see `.env.example`) and start the API on the host:
   ```bash
   python src/api.py                      # 127.0.0.1:8765
   ```
2. Start n8n and open http://localhost:5678 (create the owner account on first start):
   ```bash
   docker compose up -d n8n
   ```
   The container reaches the API at `http://host.docker.internal:8765` and sees the inbox read-only at
   `/data/inbox`.
3. Create the credential: **Credentials -> Add credential -> Header Auth**, name it
   `Invoice pipeline API`, header name `Authorization`, value `Bearer <your PIPELINE_API_TOKEN>`.
4. Import the workflow: **Workflows -> Import from File** -> `n8n/workflows/invoice_pipeline.json`.
   If a node shows a credential warning, select `Invoice pipeline API` in the three HTTP nodes.
5. **Publish** the workflow (top right). It now runs every 30 seconds.

Command-line alternative for steps 3-5 (the credential file holds the token: keep it outside the repo
and delete it afterwards):

```bash
# cred.json: [{"id": "InvPipelineApi01", "name": "Invoice pipeline API", "type": "httpHeaderAuth",
#              "data": {"name": "Authorization", "value": "Bearer <token>"}}]
docker cp cred.json invoice_n8n:/tmp/cred.json
docker exec invoice_n8n n8n import:credentials --input=/tmp/cred.json
docker exec invoice_n8n rm /tmp/cred.json
docker cp n8n/workflows/invoice_pipeline.json invoice_n8n:/tmp/wf.json
docker exec invoice_n8n n8n import:workflow --input=/tmp/wf.json
docker exec invoice_n8n n8n publish:workflow --id=invoicePipeline1   # a running n8n picks it up within seconds
```

## Try it

```bash
cp data/synthetic/pdf/doc_0002.pdf data/inbox/      # clean      -> data/processed/
cp data/synthetic/pdf/doc_0025.pdf data/inbox/      # duplicate  -> data/needs_review/ + review_log.csv
```

Executions show up under **Executions** in n8n; the API log shows each request.

## Notes

- `PIPELINE_CACHE_ONLY=1` in `.env` makes the API refuse files that have no cached extraction (409), so
  nothing is sent to the LLM; such files go to `needs_review/` with that reason.
- File names must be plain (`letters, digits, . _ -`, ending in `.pdf` or `.png`); the doc id is the
  name without the extension, so re-dropping `doc_0002.pdf` updates that invoice instead of adding one.
- Files are processed one at a time by the API. If a poll is still running when the next starts (e.g.
  many uncached files), a file can be picked up twice: the second `/process` is a cache hit and the
  second `/move` fails with 404, which shows as a failed execution but changes nothing.
- Why polling instead of a file-event trigger: n8n 2.x disables the Local File Trigger by default and file
  events are unreliable on Docker Desktop bind mounts. Polling also picks up files dropped while n8n was
  stopped.
