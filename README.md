# OZON Enterprise Product Selection Agent

Enterprise-oriented product-selection decision Agent for cross-border e-commerce scenarios.

> Current development/evaluation data is synthetic/mock data. It must not be presented as real Ozon market data.

## Why this project exists

The project targets common product-selection problems in cross-border e-commerce: fragmented product information, manual profit judgment, weak competitor comparison standards, compliance risk, and difficulty auditing AI-assisted decisions.

## Current repository structure

- `backend/` — FastAPI backend, deterministic business tools, Agent orchestration, evaluation endpoints, database models/migrations, tests.
- `extension/` — Chrome extension for Ozon-page data extraction and competitor autofill experiments.
- `demo-data/` — explicitly synthetic examples safe for portfolio use.
- `docs/evaluation/` — manual Golden Case baseline and Stability Sprint specification.
- `docs/security/` — GitHub upload/security policy for this repository.

## Current capabilities visible in the codebase

- FastAPI API layer
- Agent intent/planning and controlled tool registry
- deterministic product/profit/competition/decision logic
- enterprise product data models and Alembic migrations
- knowledge-base related endpoints/services
- evaluation service and tests
- human-review oriented decision output
- Chrome extension extraction/testing

## Development stage

**Stability Sprint v1**

The current baseline identified P0 issues around:

- Intent Routing / Fast Path
- current Agent State leakage
- Entity Resolution
- Single Source of Truth for Agent results
- Tool Policy / Tool Budget
- Recommendation Gate
- Data Sufficiency / provenance semantics
- Fallback / idempotency

See `docs/evaluation/BASELINE_MANUAL_EVAL_v1.md` and `docs/evaluation/STABILITY_SPRINT_v1.md`.

## Local setup

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Add your own API credentials only to local `.env`. Never commit `.env`.

## Data and security

This GitHub-ready package intentionally excludes:

- real `.env` and API keys
- SQLite databases and database backups
- Python virtual environments
- pytest/cache files
- runtime knowledge uploads
- local logs

Read `GITHUB_UPLOAD_MANIFEST.md` before publishing.

## Limitations

This repository is still under active development. Chat History and Long-term Memory should not be claimed as complete unless they are later implemented and regression-tested. Production SSO, secrets management, real external market connectors, and production-grade observability are outside the current portfolio baseline.
