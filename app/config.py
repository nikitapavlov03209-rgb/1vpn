from typing import List
from pydantic import AnyHttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    BOT_TOKEN: str
    ADMIN_IDS: List[int]
    REQUIRED_CHANNEL: str
    TOS_URL: AnyHttpUrl
    DATABASE_URL: str
    CRYPTOBOT_TOKEN: str
    CRYPTOBOT_PAYEE: str
    YOOKASSA_SHOP_ID: str
    YOOKASSA_SECRET_KEY: str
    BASE_PUBLIC_URL: AnyHttpUrl
    SUBSCRIPTION_SIGN_SECRET: str
    DEFAULT_DAYS: int = 30
    CURRENCY: str = "RUB"
    PRICE_MONTH: int = 399

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admins(cls, v):
        if isinstance(v, list):
            return v
        return [int(x.strip()) for x in str(v).split(",") if x.strip()]

settings = Settings()
