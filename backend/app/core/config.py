"""应用配置。

设计要点：
1. 所有可调参数集中在此，业务代码一律通过 ``settings`` 读取，禁止散落硬编码
   （AGENTS.md §7：配置集中；docs/PRD.md §16：配置项入 sys_config 或环境变量）。
2. 窗口档位（1h/24h/7d）在此定义为「档位 -> 秒数」映射，特征引擎据此拼 Redis 键；
   运行期阈值（60/80）与融合权重 α 属于业务配置，落 sys_config 表而非环境变量，
   因为策略师需要在界面上调整。
3. ``db_url`` 使用 utf8mb4，避免中文地址/商品名写入报错。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# 时间窗口档位：档位名 -> 秒数。新增档位只需在此加一行（特征注册表按档位展开）。
WINDOW_PROFILE_DEFAULT: dict[str, int] = {
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}

WINDOW_PROFILES: dict[str, dict[str, int]] = {
    "default": WINDOW_PROFILE_DEFAULT,
}


class Settings(BaseSettings):
    """从 backend/.env 读取的环境配置。"""

    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 运行环境 ----
    APP_ENV: str = "dev"
    LOG_LEVEL: str = "INFO"

    # ---- MySQL ----
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_NAME: str = "risk_control"
    DB_ECHO: bool = False

    # ---- Redis ----
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    # ---- 鉴权 ----
    JWT_SECRET: str = "dev-only-secret-change-me"
    JWT_EXPIRE_DAYS: int = 7
    JWT_ALGORITHM: str = "HS256"
    API_KEY_SEED: str = "biz-demo-api-key"

    # ---- 特征与决策 ----
    FEATURE_WINDOW_PROFILE: str = "default"

    @property
    def db_url(self) -> str:
        """SQLAlchemy 连接串（PyMySQL 驱动 + utf8mb4）。"""
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    @property
    def server_db_url(self) -> str:
        """不带库名的连接串，用于「建库」这类需要连到实例层级的操作。"""
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/?charset=utf8mb4"
        )

    @property
    def windows(self) -> dict[str, int]:
        """当前生效的窗口档位映射。档位名非法时退回默认档位并保持进程可用。"""
        return WINDOW_PROFILES.get(self.FEATURE_WINDOW_PROFILE, WINDOW_PROFILE_DEFAULT)


@lru_cache
def get_settings() -> Settings:
    """带缓存的单例，避免每次请求都重新解析 .env。"""
    return Settings()


settings = get_settings()
