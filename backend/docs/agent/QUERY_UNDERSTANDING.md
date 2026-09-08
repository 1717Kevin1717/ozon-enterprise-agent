# Query Understanding and Scoped Evidence

## Purpose

This layer separates what the user asks from the product being referenced. It reuses the existing intent policies, entity resolver, tool registry, AgentRunState, AgentRunResult and conversation state. It does not own enterprise facts.

## Contract and routing

`AgentRunResult.understanding` contains the intent, question type, entity mentions, references, metric names and user-mentioned values, requested dimensions, semantic flags, confidence, route, planned tools, planner call count, reference field and failure code. User-mentioned numeric values are not verified facts.

`parse_intent` remains the intent entry point. Source verification, formula explanation, decision explanation and freshness questions are recognized before ordinary fact/filter rules. The old generic detail default is explicitly marked unrecognized. High-confidence recognized grammar retains deterministic execution. Unsupported questions can request one semantic plan. True entity ambiguity retains the existing typed candidate clarification.

Confidence values are routing heuristics (recognized grammar versus unsupported), **not calibrated probabilities**. They must not be presented as model accuracy or evidence reliability. Existing comparison Function Calling remains compatible; its tool-result reuse tests are retained. The new semantic route itself uses at most one request and no autonomous tool loop.

## Entity spans

Catalogue names, IDs and SKUs are protected before grammar boundaries are applied. Question clauses and metric percentages are separated from names. A legitimate model or pack size such as 65W or 4件装 is preserved. Exact/normalized/alias/fuzzy matching and tenant-scoped candidate resolution remain in the existing resolver. Unsupported sentences are not sent wholesale to the catalogue as purported product names.

NOT_FOUND means a recognized query supplied a product mention which the tenant resolver could not find. AMBIGUOUS and LOW_CONFIDENCE keep distinct candidate information. Uncertain understanding or an invalid semantic plan produces clarification rather than a fabricated missing entity.

## Semantic planning boundary

The existing provider transport can make one schema-constrained planning request. Tests use Mock planners only. The request contains the user query and understanding schema, not the enterprise catalogue or historical conversations. The backend rejects unknown intents, unexpected fields, unsafe dimensions, illegal tool names, invented entity spans and missing entity/reference plans. Tool names are deduplicated; tool arguments and product IDs are constructed by the backend after tenant-scoped resolution.

The model cannot provide price, margin, risk, compliance, source metadata or recommendation decisions. These remain validated tool facts and existing decision-engine results. A valid semantic plan does not imply successful business execution. A provider failure is explicitly recorded as clarification; backend failure produces a safe error response and failed trace, not a fake fact.

## Short-term references and priority

Current explicit product mentions override current UI selection. In the absence of explicit mentions, current selection can supply the target. Otherwise a contextual question uses the current user's current tenant session's recent product IDs. Singular questions require a unique target; freshness inspection supports an explicitly selected set. A new session, another tenant or another user cannot inherit that reference.

The existing conversation state JSON stores `last_fact_dimension`. No new table, migration, long-term Memory or complete Chat History system was added. `这个价格` refers to current_price. `这个指标` can use the last fact dimension. A policy or clarification turn does not destroy the last resolved product context. No business facts are copied from prior prose.

## Response scope and truth

| Question | Projection | Fact owner |
| --- | --- | --- |
| Price source / authenticity | provenance_fact | existing field provenance and provider |
| Margin derivation | calculation_explanation | saved calculation/inputs and deterministic profit tool |
| Why not recommended | decision_explanation | existing gate status, risks and missing evidence |
| Freshness | data_quality_answer | existing FRESH / STALE / UNKNOWN field statuses |
| Does completeness imply listing approval | policy_answer | existing completeness/sufficiency and gate rules |

The compact renderer consumes this contract; it does not add recommendation cards, independent filters, metrics or decisions. Audit messages and the stored conclusion use the final validated answer. Explanation projections do not change formulas or gates.

## Provenance and learning notes

Completeness measures presence. Sufficiency measures whether evidence can support a specific inference. Freshness measures age relative to a field's validity window. Provenance records where the field came from. Recommendation is a gated business conclusion. None is a substitute for another.

Reported sales growth remains distinct from the observed snapshot trend. One valid snapshot cannot establish growth or decline. Mock data and platform reference URLs do not prove an actual Ozon observation. Formula explanations surface saved inputs, including unknown/legacy evidence; they do not reconstruct an undocumented historical input chain.

## Evaluation and limitations

Run `python -m evals.runner --category semantic_understanding` in backend for the semantic subset, or `python -m evals.runner` for the full suite. Workers use isolated memory databases and block dotenv, credentials and external HTTP. Judges are deterministic; ground truth is human ACT5 observations plus specified business rules, not model-generated answers.

This is a bounded grammar and controlled fallback, not general natural-language understanding. The catalogue resolver currently examines the existing maximum of 500 tenant products. No live GLM stability or arbitrary-paraphrase accuracy is claimed. Mock Dataset != real Ozon data. Not Production Ready.
