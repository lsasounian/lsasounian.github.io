"""Config centralizada, lida de variáveis de ambiente (.env em dev)."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    aws_region: str = "us-east-1"
    bedrock_model_id: str = "anthropic.claude-sonnet-4-5-20250929-v1:0"

    mcp_server_url: str = "http://mcp-server:8001/mcp"

    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    otel_service_name: str = "mediator-agent"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
