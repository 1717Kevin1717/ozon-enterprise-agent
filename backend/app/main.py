from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.api.router import router
from app.api.knowledge_router import knowledge_router
from app.api.evaluation_router import evaluation_router
from app.api.product_import_router import product_import_router
from app.core.config import ROOT, settings
from app.core.tenant_context import require_local_demo_database
from app.db.models import Base, Company
from app.db.session import SessionLocal, engine

@asynccontextmanager
async def lifespan(app: FastAPI):
    demo_mode = settings.app_env in {"development", "test"} and settings.demo_identity_enabled
    if demo_mode:
        require_local_demo_database(settings.database_url, workspace=ROOT, test_mode=settings.app_env == "test")
    Path(ROOT / "data").mkdir(exist_ok=True)
    async with engine.begin() as connection: await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        if demo_mode and not await session.get(Company,settings.default_company_id): session.add(Company(id=settings.default_company_id,name="本地中贸通试点企业")); await session.commit()
    yield
    await engine.dispose()

app=FastAPI(title="中贸通 · Ozon AI 企业选品智能体",version="0.1.0",lifespan=lifespan)
origins=[item.strip() for item in settings.cors_origins.split(",") if item.strip()]
allowed_origins=["*"] if settings.app_env == "development" else origins
app.add_middleware(CORSMiddleware,allow_origins=allowed_origins,allow_credentials=False,allow_methods=["GET","POST","PATCH","DELETE"],allow_headers=["Content-Type","X-Company-ID","X-User-ID","X-Role"] if settings.app_env in {"development", "test"} else ["Content-Type", "Authorization"])
app.include_router(router)
app.include_router(knowledge_router)
app.include_router(evaluation_router)
app.include_router(product_import_router)
app.mount("/static", StaticFiles(directory=ROOT / "app" / "web"), name="static")

@app.get("/",include_in_schema=False)
async def home(): return FileResponse(ROOT / "app" / "web" / "index.html",media_type="text/html")
