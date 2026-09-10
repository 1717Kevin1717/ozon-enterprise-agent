# Conversation Context

## Scope

This project implements structured short-term references inside one Agent session. It is not Long-term Memory and not a complete Chat History product. Product facts remain tenant-scoped Backend data and are never copied into the context snapshot.

## ContextSnapshot

`ContextSnapshot` is built once at the beginning of each turn from the authenticated company/user, the verified `ConversationSession.state_json`, the current query, and valid UI-selected product IDs. It contains:

- session identity and turn index;
- current UI selection plus its revision/session binding;
- last explicit and last resolved product IDs;
- last intent, response type, requested dimensions, metric and fact dimension;
- last comparison IDs and stable input order;
- references to successful tool results, never the result bodies;
- warnings for deleted, inaccessible or cross-tenant IDs.

Prices, profit, ROI, risk, compliance, recommendation status and provenance are not session facts. The Agent must read them again through Backend tools.

## Context Sources

Targets use one typed source:

1. `explicit_query`
2. `ui_selection`
3. `ordinal_reference`
4. `last_explicit_entity`
5. `last_comparison`
6. `last_resolved_entity`
7. `session_state`
8. `none`

The response keeps the older `selection_source` values for API compatibility and also publishes `context_source` for the unambiguous v1 contract.

## Resolution Priority

Current explicit product ID/name always wins. Explicit UI-selection language is next. Ordinals bind only to `last_comparison_order`. A singular pronoun uses a one-item session-bound current selection, otherwise the last unique explicit/resolved entity. An elliptical metric follow-up may inherit the prior entity and fact dimension. Sources are not silently merged.

An invalid, deleted or cross-tenant product ID is dropped before resolution. Missing plural selection, missing comparison order or a non-unique singular referent produces clarification rather than guessing.

## Session Boundary

`ConversationSession.state_json` is the persistence boundary. A new session starts with no explicit/resolved entity, metric, comparison order or tool references. The frontend may preserve its global Compare workspace, but `newAgentSession()` clears the Agent-bound selection and last rendered result.

The global workspace enters a turn only when the query explicitly refers to selected products, such as “这几个”, “我选的这些”, “用对比中心” or “现在比较一下”. One compatibility exception remains: a price-scenario query may use exactly one current selection because the deterministic simulation requires one target. Generic policy questions never consume product context.

## Dimension Inheritance

Dimension inheritance is permitted only for an elliptical continuation. “换成 X 呢” can inherit `price` from the preceding price fact; “那利润呢” explicitly changes the current scope to profit; “这个利润怎么算” becomes a calculation explanation. Stored dimensions never expand an unrelated new query.

## Semantic Router

The router retains three outcomes:

- high-confidence deterministic questions use the existing Fast Path;
- context-rich or semantically complex questions may use the Semantic Planner;
- clarification is reserved for genuinely missing, conflicting or non-unique context.

If no semantic planner is available, a recognized deterministic/contextual intent can still execute locally. A genuinely unknown question remains clarification.

## Semantic Context Packet

The internal `SemanticContextPacket` contains task metadata, verified identity references, context roles, comparison order, previous dimensions and allowed tools. Mock Semantic Planner tests receive this typed packet. The external GLM adapter continues to send only the existing query until a separately approved data-egress contract is defined; tenant product IDs/names are not newly transmitted by this Sprint.

## Grounded Synthesis

LLM planning may interpret language, references, preferences and requested dimensions. It cannot establish product existence, price, profit, risk, compliance, provenance, permissions or Recommendation Gate status. Those facts and policies come from deterministic Backend tools and the validated `AgentRunResult`.

Successful context updates happen only after response validation. Tool results remain protected by the existing `AgentRunState` cache, policy, timeout and idempotency rules.

## Failure Semantics

- `ENTITY_NOT_FOUND`: a high-confidence explicit product span has no tenant candidate.
- `ENTITY_AMBIGUOUS` / `LOW_CONFIDENCE`: explicit entity resolution cannot be unique.
- `REFERENCE_UNRESOLVED`: a pronoun has no unique referent.
- `MISSING_UI_SELECTION`: selection language is present but the workspace is empty.
- `MISSING_COMPARISON_CONTEXT`: a comparison continuation has no prior comparison.
- `ORDINAL_OUT_OF_RANGE`: an ordinal is outside the saved comparison order.
- `MISSING_CONTEXT`: an elliptical task lacks a prior referent.

None of these failures authorizes the LLM or Renderer to invent a product or business fact.
