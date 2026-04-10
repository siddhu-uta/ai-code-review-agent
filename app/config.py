from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    anthropic_api_key: str
    github_token: str
    aws_region: str = "us-east-1"
    dynamodb_table: str = "code-reviews"
    api_secret_key: str


settings = Settings()
