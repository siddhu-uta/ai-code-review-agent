from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_token: str
    aws_region: str = "us-east-1"
    dynamodb_table: str = "code-reviews"
    api_secret_key: str
    # Cross-region inference prefix gives better availability across AZs
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250514-v1:0"


settings = Settings()
