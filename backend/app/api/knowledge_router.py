from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.router import context, require_role
from app.db.session import get_session
from app.schemas.knowledge import KnowledgeSearchRequest
from app.services.knowledge_base import KnowledgeError, document_view, ingest_document, list_documents, search_documents

knowledge_router = APIRouter(prefix="/api/v1/knowledge", tags=["company-knowledge"])


@knowledge_router.get("/documents")
async def documents(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    return {"success": True, "data": [document_view(item) for item in await list_documents(session, ctx["company_id"])]}


@knowledge_router.post("/documents", status_code=201)
async def upload_document(file: UploadFile = File(...), ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx, "operator")
    content = await file.read()
    try:
        document, created = await ingest_document(session, ctx["company_id"], file.filename or "unnamed", file.content_type or "", content)
    except KnowledgeError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": exc.message}) from exc
    return {"success": True, "data": {"document": document_view(document), "created": created, "notice": "文档已在本企业范围内解析；当前使用本地哈希向量，不调用外部 Embedding 服务。"}}


@knowledge_router.post("/search")
async def search(data: KnowledgeSearchRequest, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    return {"success": True, "data": {"matches": await search_documents(session, ctx["company_id"], data.query, data.limit), "mode": "local_hash_embedding_v1", "notice": "第一版用于可复现本地检索；上线前应接企业批准的语义 Embedding 与检索评测。"}}
