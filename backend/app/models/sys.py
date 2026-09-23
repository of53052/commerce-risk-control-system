"""系统域模型：账号、配置、业务端 API Key。

设计要点：
- 角色用字符串枚举（auditor/strategist/admin）而非数字位掩码：单人维护的系统里，
  可读性比省几个字节重要得多。
- 阈值（60/80）、融合权重 α、合案窗口等"策略师会改的参数"放 sys_config 表；
  JWT 密钥、数据库口令这类"部署参数"留在 .env。两者边界清晰，避免混用。
"""

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.db.base import Base

ROLE_AUDITOR = "auditor"
ROLE_STRATEGIST = "strategist"
ROLE_ADMIN = "admin"

STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"


class SysUser(Base):
    """后台账号（预置 3 个角色账号，见 docs/PRD.md §4.3）。"""

    __tablename__ = "sys_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="登录名")
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False, comment="bcrypt 摘要")
    real_name: Mapped[str | None] = mapped_column(String(64), comment="展示名")
    role: Mapped[str] = mapped_column(String(32), nullable=False, comment="auditor/strategist/admin")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ENABLED, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SysConfig(Base):
    """运行期可配参数（可用界面修改，改动入审计）。

    读取方式：sys_config 里没有的键，由业务代码使用内置默认值兜底，
    这样"清空配置表"不会让系统不可用。
    """

    __tablename__ = "sys_config"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    config_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    config_value: Mapped[str] = mapped_column(Text, nullable=False, comment="统一按字符串存储，读取方自行转型")
    value_type: Mapped[str] = mapped_column(String(16), default="str", nullable=False, comment="str/int/float/bool/json")
    description: Mapped[str | None] = mapped_column(String(255))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SysApiKey(Base):
    """业务端调用凭证。

    只存哈希：即使库被拖走也无法直接调用。明文只在初始化时写入 .env 的 API_KEY_SEED，不落库。

    为什么用 SHA-256 而不是 bcrypt（重要取舍）：
        口令是「低熵、可枚举」的，必须用慢哈希（bcrypt）抵御离线爆破；
        而 API Key 是 32 字节随机串（约 256 bit 熵），爆破在数学上不可行，
        此时 bcrypt 带来的开销（cost=12 约 250ms/次）纯属浪费 —— 它会被加在
        **每一次事件接入请求**上，直接冲垮 docs/PRD.md §16「单次决策 P95 < 50ms」。
        改用 SHA-256 还有一个附带好处：摘要确定，可建立唯一索引后按哈希做等值查找，
        而不必把全部 Key 取出来逐个比对。
    """

    __tablename__ = "sys_api_key"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="调用方名称，如「模拟业务端」")
    api_key_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, comment="sha256(明文) 的十六进制摘要"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[object | None] = mapped_column(DateTime, comment="最近一次调用时间，便于识别僵尸密钥")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
