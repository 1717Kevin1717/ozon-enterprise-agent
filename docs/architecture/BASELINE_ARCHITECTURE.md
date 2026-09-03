# Architecture Snapshot — Baseline before Stability Sprint v1

Observed repository layers:

```text
Browser / Web UI / Chrome Extension
            ↓
        FastAPI API
            ↓
      Agent orchestration
   Intent / Planner / Tool Registry
            ↓
 Deterministic enterprise tools/services
            ↓
 Repository / SQLAlchemy / Database
            ↓
 Structured decision + human review UI
```

The baseline evaluation shows that the deterministic business layer can produce useful results, but orchestration currently needs stabilization: explicit current queries must outrank stale selected-product state, resolved product identity should converge to a unique `product_id`, and the UI/summary/audit should consume one validated structured run result.

This document describes the baseline only. It does not claim that Chat History or Long-term Memory are complete.
