from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    database_url: str = "sqlite+aiosqlite:///./data/zmt_enterprise.db"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "http://127.0.0.1:8000,http://localhost:8000"
    default_company_id: str = "local-zmt"
    default_user_id: str = "local-operator"
    default_role: str = "company_admin"
    max_capture_bytes: int = 2 * 1024 * 1024
    llm_provider: str = "disabled"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    embedding_model: str = ""
    primary_llm_provider: str = "qwen"
    primary_llm_model: str = "qwen3.5-plus"
    reasoning_llm_provider: str = "deepseek"
    reasoning_llm_model: str = "deepseek-v4-pro"
    qwen_api_key: str = ""
    dashscope_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    provider_timeout_seconds: int = 30
    external_reasoning_data_mode: str = "disabled"
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_model: str = "glm-5.2"
    zhipu_max_tool_rounds: int = 4
    zhipu_timeout_seconds: int = 45
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

settings = Settings()
