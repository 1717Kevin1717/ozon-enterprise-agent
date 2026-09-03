import uuid
from datetime import UTC, datetime
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

def uid() -> str: return str(uuid.uuid4())
def now() -> datetime: return datetime.now(UTC).replace(tzinfo=None)

class Base(DeclarativeBase):
    pass

class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

class Company(Base, Timestamped):
    __tablename__ = "companies"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active")

class User(Base, Timestamped):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    status: Mapped[str] = mapped_column(String(32), default="active")

class Store(Base, Timestamped):
    __tablename__ = "stores"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="ozon")
    store_name: Mapped[str] = mapped_column(String(255), nullable=False)
    external_store_id: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")

class Product(Base, Timestamped):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("company_id", "external_product_id", name="uq_product_company_external"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("stores.id"), nullable=True)
    external_product_id: Mapped[str] = mapped_column(String(128), default="")
    sku: Mapped[str] = mapped_column(String(128), default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    brand: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(255), default="")
    category_path: Mapped[str] = mapped_column(String(1000), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    main_image_url: Mapped[str] = mapped_column(Text, default="")
    image_urls: Mapped[list] = mapped_column(JSON, default=list)
    seller_name: Mapped[str] = mapped_column(String(255), default="")
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    current_price: Mapped[float] = mapped_column(Float, default=0)
    regular_price: Mapped[float] = mapped_column(Float, default=0)
    competitor_price_min: Mapped[float] = mapped_column(Float, default=0)
    competitor_price_avg: Mapped[float] = mapped_column(Float, default=0)
    price_position: Mapped[str] = mapped_column(String(32), default="unknown")
    rating: Mapped[float] = mapped_column(Float, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_30d_sales: Mapped[float] = mapped_column(Float, default=0)
    sales_growth_rate: Mapped[float] = mapped_column(Float, default=0)
    search_volume: Mapped[float] = mapped_column(Float, default=0)
    trend_score: Mapped[float] = mapped_column(Float, default=0)
    competitor_count: Mapped[int] = mapped_column(Integer, default=0)
    price_competition_score: Mapped[float] = mapped_column(Float, default=0)
    market_saturation: Mapped[float] = mapped_column(Float, default=0)
    compliance_status: Mapped[str] = mapped_column(String(32), default="pending")
    compliance_note: Mapped[str] = mapped_column(Text, default="")
    certificates: Mapped[list] = mapped_column(JSON, default=list)
    manual_risk_level: Mapped[str] = mapped_column(String(32), default="unknown")
    procurement_cost: Mapped[float] = mapped_column(Float, default=0)
    fulfillment_cost: Mapped[float] = mapped_column(Float, default=0)
    shipping_cost: Mapped[float] = mapped_column(Float, default=0)
    platform_fee: Mapped[float] = mapped_column(Float, default=0)
    advertising_cost: Mapped[float] = mapped_column(Float, default=0)
    warehousing_cost: Mapped[float] = mapped_column(Float, default=0)
    tax_cost: Mapped[float] = mapped_column(Float, default=0)
    return_loss_reserve: Mapped[float] = mapped_column(Float, default=0)
    other_cost: Mapped[float] = mapped_column(Float, default=0)
    platform_commission_rate: Mapped[float] = mapped_column(Float, default=0)
    target_margin_rate: Mapped[float] = mapped_column(Float, default=0.30)
    current_margin_rate: Mapped[float] = mapped_column(Float, default=0)
    lifecycle_status: Mapped[str] = mapped_column(String(32), default="candidate")
    data_quality: Mapped[str] = mapped_column(String(32), default="partial")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    field_lineage: Mapped[dict] = mapped_column(JSON, default=dict)
    sales_source: Mapped[str] = mapped_column(String(128), default="not_provided")
    sales_evidence: Mapped[str] = mapped_column(Text, default="")
    competition_evidence: Mapped[str] = mapped_column(Text, default="")
    sync_status: Mapped[str] = mapped_column(String(32), default="not_synced")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)

class ProductSnapshot(Base):
    __tablename__ = "product_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    price: Mapped[float] = mapped_column(Float, default=0)
    rating: Mapped[float] = mapped_column(Float, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    sales_30d: Mapped[float] = mapped_column(Float, default=0)
    competitor_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)

class ProductAnalysis(Base):
    __tablename__ = "product_analyses"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="v1.0.0")
    demand_score: Mapped[float] = mapped_column(Float, default=0)
    profit_score: Mapped[float] = mapped_column(Float, default=0)
    competition_score: Mapped[float] = mapped_column(Float, default=0)
    compliance_score: Mapped[float] = mapped_column(Float, default=0)
    risk_score: Mapped[float] = mapped_column(Float, default=0)
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommendation_grade: Mapped[str] = mapped_column(String(8), default="C")
    risk_level: Mapped[str] = mapped_column(String(32), default="high")
    gross_profit: Mapped[float] = mapped_column(Float, default=0)
    gross_margin: Mapped[float] = mapped_column(Float, default=0)
    net_profit: Mapped[float] = mapped_column(Float, default=0)
    net_margin: Mapped[float] = mapped_column(Float, default=0)
    roi: Mapped[float] = mapped_column(Float, default=0)
    data_completeness: Mapped[float] = mapped_column(Float, default=0)
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    score_explanations: Mapped[dict] = mapped_column(JSON, default=dict)
    risks_json: Mapped[list] = mapped_column(JSON, default=list)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    weights_json: Mapped[dict] = mapped_column(JSON, default=dict)
    algorithm_version: Mapped[str] = mapped_column(String(32), default="v1.0.0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class ProductDecision(Base):
    __tablename__ = "product_decisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    recommendation: Mapped[str] = mapped_column(String(32), default="pending")
    decision_status: Mapped[str] = mapped_column(String(32), default="pending")
    reason: Mapped[str] = mapped_column(Text, default="")
    reviewer_id: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(64), default="human")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class ProductRelation(Base, Timestamped):
    __tablename__ = "product_relations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    source_product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    target_product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="human_confirmed")

class ConversationSession(Base, Timestamped):
    __tablename__ = "conversation_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="新建分析会话")
    goal_summary: Mapped[str] = mapped_column(Text, default="")
    state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_product_ids: Mapped[list] = mapped_column(JSON, default=list)
    conclusion_summary: Mapped[str] = mapped_column(Text, default="")
    memory_version: Mapped[str] = mapped_column(String(32), default="v1")

class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("conversation_sessions.id"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    query: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="rule-based-local")
    active_provider: Mapped[str] = mapped_column(String(64), default="rule_engine")
    response_mode: Mapped[str] = mapped_column(String(64), default="rule_engine")
    fallback_reason: Mapped[str] = mapped_column(String(128), default="")
    intent: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="v1")
    tool_registry_version: Mapped[str] = mapped_column(String(32), default="v1")
    tools_used: Mapped[list] = mapped_column(JSON, default=list)
    trace_json: Mapped[list] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    token_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class KnowledgeDocument(Base, Timestamped):
    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("company_id", "checksum", name="uq_knowledge_document_company_checksum"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    source_type: Mapped[str] = mapped_column(String(64), default="user_upload")
    storage_path: Mapped[str] = mapped_column(Text, default="")
    checksum: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(128), default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_document_index"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), index=True, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class ConnectorRun(Base):
    __tablename__ = "connector_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    connector_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    records_requested: Mapped[int] = mapped_column(Integer, default=0)
    records_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(128), default="")
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class AgentEvaluation(Base):
    __tablename__ = "agent_evaluations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), index=True, nullable=True)
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    task_input: Mapped[dict] = mapped_column(JSON, default=dict)
    expected_output: Mapped[dict] = mapped_column(JSON, default=dict)
    actual_output: Mapped[dict] = mapped_column(JSON, default=dict)
    task_success_rate: Mapped[float] = mapped_column(Float, default=0)
    answer_accuracy: Mapped[float] = mapped_column(Float, default=0)
    tool_calling_accuracy: Mapped[float] = mapped_column(Float, default=0)
    data_completeness: Mapped[float] = mapped_column(Float, default=0)
    judge_mode: Mapped[str] = mapped_column(String(32), default="deterministic")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class PromptTemplate(Base, Timestamped):
    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("company_id", "name", "version", name="uq_prompt_company_name_version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    schema_version: Mapped[str] = mapped_column(String(32), default="v1")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
