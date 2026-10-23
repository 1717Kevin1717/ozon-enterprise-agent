"""Two-step merchant CSV ingestion. Identity always comes from trusted context."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.router import context, require_role
from app.connectors.merchant_csv import MAX_CSV_BYTES, MerchantCsvError
from app.db.session import get_session
from app.services.product_import import ImportConflict, confirm_import, preview_import


product_import_router = APIRouter(prefix="/api/v1/products/import", tags=["merchant-import"])


async def _content(file: UploadFile) -> bytes:
    content = await file.read(MAX_CSV_BYTES + 1)
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(413, detail={"code": "FILE_SIZE_INVALID", "message": "CSV 文件过大。"})
    return content


@product_import_router.post("/preview")
async def preview(file: UploadFile = File(...), ctx: dict = Depends(context),
                  session: AsyncSession = Depends(get_session)):
    require_role(ctx, "operator")
    try:
        result = await preview_import(session, ctx["company_id"], ctx["user_id"], await _content(file))
    except MerchantCsvError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": "CSV 文件格式或内容不符合导入要求。"}) from exc
    return {"success": True, "data": result.model_dump(mode="json")}


@product_import_router.post("/confirm")
async def confirm(preview_id: str = Form(...), file: UploadFile = File(...),
                  ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx, "operator")
    try:
        result = await confirm_import(session, ctx["company_id"], ctx["user_id"], preview_id, await _content(file))
    except MerchantCsvError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": "CSV 文件格式或内容不符合导入要求。"}) from exc
    except ImportConflict as exc:
        raise HTTPException(409, detail={"code": exc.code, "message": "预览或商品状态已变化，请重新预览。"}) from exc
    return {"success": True, "data": result.model_dump(mode="json")}
