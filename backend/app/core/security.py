"""安全工具：口令哈希、JWT 签发与校验、业务端 API Key 校验。

三条取舍（都是「踩过坑才会想到」的那种）：

1. **口令与 API Key 用不同的哈希算法，这不是不一致，而是刻意为之。**
   口令是低熵、可枚举的（"admin123" 就在字典里），必须用慢哈希 bcrypt 抵御离线爆破；
   API Key 是高熵随机串，爆破不可行，用 SHA-256。
   反过来若给 API Key 上 bcrypt，等于给每一次事件接入请求加 250ms 延迟，
   与 docs/PRD.md §16「P95 < 50ms」直接冲突（详见 app/models/sys.py 的 SysApiKey 注释）。

2. **JWT 只携带 sub / username / role，不放权限明细。**
   权限矩阵在后端按角色硬编码校验（docs/PRD.md §4.2）。
   把权限塞进令牌意味着「改了权限要等令牌过期才生效」，是权限事故的常见来源。

3. **bcrypt 的 72 字节上限必须显式挡住。**
   bcrypt 4.x 对超长输入直接抛 ValueError；不挡的话，一个超长口令会让登录接口返回 500
   而不是 400。这里在哈希前显式校验并抛出可读异常，由接口层转成参数错误。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings

# bcrypt 的硬上限（字节，非字符数）：超过即拒绝，不做静默截断。
# 之所以不截断：截断会让"两个不同的长口令"验签相同，属于安全隐患。
BCRYPT_MAX_BYTES = 72

# API Key 明文长度：token_urlsafe(32) 约 43 个字符，约 256 bit 熵。
API_KEY_BYTES = 32

# HMAC 类算法（HS256/HS384/HS512）的密钥长度下限（字节）。
# 取值来自 RFC 7518 §3.2：密钥长度不应小于哈希输出长度，HS256 即 32 字节。
MIN_HMAC_SECRET_BYTES = 32


class InvalidTokenError(Exception):
    """令牌无效或已过期。单独定义异常类型，便于接口层统一映射为 401。"""


class PasswordTooLongError(ValueError):
    """口令超过 bcrypt 可处理长度。"""


# --------------------------------------------------------------------------- #
# 口令
# --------------------------------------------------------------------------- #
def hash_password(raw: str) -> str:
    """计算 bcrypt 摘要（用于 sys_user.password_hash）。"""
    payload = raw.encode("utf-8")
    if len(payload) > BCRYPT_MAX_BYTES:
        raise PasswordTooLongError(
            f"口令过长：{len(payload)} 字节，bcrypt 上限 {BCRYPT_MAX_BYTES} 字节"
        )
    return bcrypt.hashpw(payload, bcrypt.gensalt()).decode("ascii")


def verify_password(raw: str, hashed: str) -> bool:
    """校验口令。

    任何异常都返回 False：库里存了脏数据（手工改坏、算法换过）时应当"验签不通过"，
    而不是让登录接口抛 500 —— 后者会把"数据坏了"暴露成"服务不可用"。
    """
    try:
        payload = raw.encode("utf-8")
        if len(payload) > BCRYPT_MAX_BYTES:
            return False
        return bcrypt.checkpw(payload, hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def create_access_token(
    user_id: int,
    username: str,
    role: str,
    expires_days: int | None = None,
) -> tuple[str, datetime]:
    """签发访问令牌，返回 (token, 过期时间)。

    过期时间一并返回，是为了让登录接口直接把它返给前端做"提前续期"判断，
    避免前端自己解析 JWT 造成时间口径不一致（前端时区不可信）。

    **HS256 的密钥长度必须 ≥ 32 字节**：更短的密钥会让 PyJWT 每次签发/校验都打
    ``InsecureKeyLengthWarning``（RFC 7518 §3.2 的建议下限），
    而告警刷屏会掩盖真正需要关注的安全提示。这里在签发前显式拦一次，
    把"配置有问题"变成启动期就能发现的错误，而不是运行期的一条条告警。
    """
    secret_bytes = settings.JWT_SECRET.encode("utf-8")
    if settings.JWT_ALGORITHM.startswith("HS") and len(secret_bytes) < MIN_HMAC_SECRET_BYTES:
        raise ValueError(
            f"JWT_SECRET 过短（{len(secret_bytes)} 字节）；"
            f"{settings.JWT_ALGORITHM} 要求至少 {MIN_HMAC_SECRET_BYTES} 字节。"
            "请在 backend/.env 中改为随机长字符串。"
        )
    days = expires_days if expires_days is not None else settings.JWT_EXPIRE_DAYS
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=days)
    payload = {
        "sub": str(user_id),          # PyJWT 规范要求 sub 为字符串
        "username": username,
        "role": role,
        "iat": now,
        "exp": expire,
        "jti": secrets.token_hex(8),  # 便于日后做吊销名单 / 排查同一秒签发的多个令牌
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expire.replace(tzinfo=None)


def decode_access_token(token: str) -> dict:
    """解码并校验令牌，失败抛 InvalidTokenError。"""
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],  # 显式指定算法，防 alg=none 降级攻击
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# 业务端 API Key
# --------------------------------------------------------------------------- #
def generate_api_key() -> str:
    """生成一个新的 API Key 明文（仅在需要轮换时使用）。"""
    return secrets.token_urlsafe(API_KEY_BYTES)


def hash_api_key(raw: str) -> str:
    """SHA-256 十六进制摘要。确定性摘要使「按哈希等值查找」成为可能。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_api_key(raw: str, stored_hash: str) -> bool:
    """常数时间比对 API Key 摘要，避免时序侧信道。"""
    return hmac.compare_digest(hash_api_key(raw), stored_hash)
