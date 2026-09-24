"""特征引擎：声明式注册表 + 窗口聚合。

核心设计（docs/ARCHITECTURE.md 7.1）：**新增特征只加一条声明，不写流程代码**。

一个 ``FeatureSpec`` 描述"从哪取、按什么聚合、窗口多长"，
引擎负责拼键、取数、组装扁平特征字典。这样 27 个特征的实现成本约等于 27 行声明，
而不是 27 个函数。

特征键命名规则（与 docs/PRD.md 7.2 一致）::

    {base}_{window}       例如 device_account_cnt_24h
    {base}                无窗口特征（画像/标记类）

聚合算子（``agg``）:
    count     窗口内事件条数（ZCOUNT）
    sum       窗口内某金额字段求和（遍历条带成员）
    distinct  窗口内不同主体数（条带 member 携带主体标识）
    ratio     两个已算出的特征相除（第二遍计算，见 _compute_derived）
    flag      布尔标记（来自名单服务或业务表，非窗口聚合）
    profile   来自业务表/规则计算的画像值（如账号注册天数）

**窗口边界一律用事件自身时间戳**（``ts_ms``），不用服务器当前时间 ——
这是历史重放结果与实时决策结果一致的前提（docs/ARCHITECTURE.md 7.1）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.timeutil import to_ms, utcnow
from app.models.biz import BizCustomer, BizOrder
from app.models.event import (
    EVENT_AFTER_SALE_APPLY,
    EVENT_COUPON_RECEIVE,
    EVENT_LOGIN,
    EVENT_ORDER_CREATE,
    EVENT_ORDER_PAY,
)
from app.services import window_store
from app.services.config_service import get_value
from app.services.flags import BLACKLIST_FLAG_KEY, FLAG_FEATURE_KEYS, GRAY_FLAG_KEYS, WHITELIST_FLAG_KEY

logger = logging.getLogger("app.services.feature_engine")

FEATURE_VERSION = "v1"

# 默认窗口档位（sys_config 不可用时的兜底；与 app/core/config.py 保持一致）
DEFAULT_WINDOWS: dict[str, int] = {"1h": 3600, "24h": 86400, "7d": 604800}

Agg = Literal["count", "sum", "distinct", "ratio", "flag", "profile"]


@dataclass(frozen=True)
class FeatureSpec:
    """一条特征声明。"""

    key: str
    agg: Agg
    entity: str | None = None
    event_types: tuple[str, ...] = ()
    windows: tuple[str, ...] = ()
    amount_field: str | None = None
    subject_prefix: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    description: str = ""

    def keys(self, windows: dict[str, int]) -> list[str]:
        """展开出该声明实际产出的特征键（只取当前档位存在的窗口）。"""
        if not self.windows:
            return [self.key]
        return [f"{self.key}_{window}" for window in self.windows if window in windows]


# --------------------------------------------------------------------------- #
# 特征注册表（docs/PRD.md 7.2 的 27 个特征键）
# --------------------------------------------------------------------------- #
# 名单标记的特征声明：键名由 app/services/flags.py 提供（名单服务的产出物），
# 但**声明必须与注册表同源** —— `is_feature_key` 与 `known_feature_keys`
# 都以本注册表为准，若这些键靠"别人导入时回填"，任何一条不经过
# app.models 的调用路径（脚本、单测、将来的 worker）都会把
# subject_blacklist 误判成"上下文"，于是特征快照里少了它、模型也读不到，
# 而这一切不会报错。宁可多写这几行，也不要引入导入序依赖。
LIST_FLAG_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(BLACKLIST_FLAG_KEY, "flag", description="主体是否命中黑名单（名单服务产出，直接决定动作）"),
    FeatureSpec(WHITELIST_FLAG_KEY, "flag", description="主体是否命中白名单（名单服务产出，直接决定动作）"),
    *(
        FeatureSpec(key, "flag", description=f"名单维度 {dimension} 是否命中灰名单（加成特征）")
        for dimension, key in GRAY_FLAG_KEYS.items()
    ),
)

FEATURE_REGISTRY: tuple[FeatureSpec, ...] = (
    # ---- 用户行为频次 ----
    FeatureSpec("user_login_cnt", "count", entity="user", event_types=(EVENT_LOGIN,), windows=("1h", "24h", "7d")),
    FeatureSpec("user_coupon_cnt", "count", entity="user", event_types=(EVENT_COUPON_RECEIVE,), windows=("1h", "24h", "7d")),
    FeatureSpec("user_coupon_amount", "sum", entity="user", event_types=(EVENT_COUPON_RECEIVE,), windows=("24h",), amount_field="face_value"),
    FeatureSpec("user_order_cnt", "count", entity="user", event_types=(EVENT_ORDER_CREATE,), windows=("1h", "24h", "7d")),
    FeatureSpec("user_order_amount", "sum", entity="user", event_types=(EVENT_ORDER_CREATE,), windows=("24h",), amount_field="amount"),
    FeatureSpec("user_refund_cnt", "count", entity="user", event_types=(EVENT_AFTER_SALE_APPLY,), windows=("24h", "7d")),
    FeatureSpec("user_refund_amount", "sum", entity="user", event_types=(EVENT_AFTER_SALE_APPLY,), windows=("24h",), amount_field="refund_amount"),
    FeatureSpec(
        "user_refund_rate",
        "ratio",
        windows=("24h",),
        numerator="user_refund_cnt",
        denominator="user_order_cnt",
        description="退款申请数 / 订单数；分母为 0 时取 0",
    ),
    FeatureSpec("user_first_order_refund", "flag", description="首单即退款标记"),

    # ---- 设备环境 ----
    FeatureSpec(
        "device_account_cnt",
        "distinct",
        entity="device",
        event_types=(EVENT_LOGIN, EVENT_COUPON_RECEIVE, EVENT_ORDER_CREATE, EVENT_ORDER_PAY),
        windows=("24h", "7d"),
        subject_prefix="user",
    ),
    FeatureSpec("device_coupon_cnt", "count", entity="device", event_types=(EVENT_COUPON_RECEIVE,), windows=("1h",)),
    FeatureSpec("device_order_cnt", "count", entity="device", event_types=(EVENT_ORDER_CREATE,), windows=("1h", "24h")),
    FeatureSpec(
        "device_new_account_ratio",
        "ratio",
        windows=("24h",),
        numerator="device_new_account_cnt",
        denominator="device_account_cnt",
        description="同设备新账号（注册 < 7 天）占比",
    ),
    FeatureSpec("device_env_risk", "profile", description="环境指纹风险分（0-100）"),

    # ---- 网络 IP ----
    FeatureSpec(
        "ip_account_cnt",
        "distinct",
        entity="ip",
        event_types=(EVENT_LOGIN, EVENT_COUPON_RECEIVE, EVENT_ORDER_CREATE, EVENT_ORDER_PAY),
        windows=("24h", "7d"),
        subject_prefix="user",
    ),
    FeatureSpec("ip_coupon_cnt", "count", entity="ip", event_types=(EVENT_COUPON_RECEIVE,), windows=("1h",)),
    FeatureSpec("ip_order_cnt", "count", entity="ip", event_types=(EVENT_ORDER_CREATE,), windows=("1h",)),
    FeatureSpec("ip_region_mismatch", "profile", description="IP 归属地与收货地不一致"),
    FeatureSpec("ip_is_datacenter", "profile", description="IP 是否数据中心/代理段"),

    # ---- 收货地址 ----
    FeatureSpec("address_account_cnt", "distinct", entity="address", event_types=(EVENT_ORDER_CREATE,), windows=("7d",), subject_prefix="user"),
    FeatureSpec("address_refund_cnt", "count", entity="address", event_types=(EVENT_AFTER_SALE_APPLY,), windows=("7d",)),
    FeatureSpec("address_order_cnt", "count", entity="address", event_types=(EVENT_ORDER_CREATE,), windows=("7d",), description="同地址下单数（退款率分母，非独立展示特征）"),
    FeatureSpec("address_refund_rate", "ratio", windows=("7d",), numerator="address_refund_cnt", denominator="address_order_cnt", description="同收货地址退款率"),
    FeatureSpec("address_phone_share_cnt", "profile", description="同收货地址关联账号数"),

    # ---- 主体画像 ----
    FeatureSpec("account_age_days", "profile", description="账号注册天数"),
    FeatureSpec("subject_case_cnt", "profile", description="近 30 天该主体风险案件数（P1 案件表就绪后接入）"),
    FeatureSpec("night_activity_ratio", "profile", description="夜间（0-6 点）行为占比（P0 为当前事件二值化口径）"),
    FeatureSpec("device_new_account_cnt", "profile", description="同设备新账号数（device_new_account_ratio 的分子）"),

    # ---- 名单标记（键名见 app/services/flags.py）----
    *LIST_FLAG_SPECS,
)

FEATURE_SPECS_BY_KEY: dict[str, FeatureSpec] = {spec.key: spec for spec in FEATURE_REGISTRY}

# ---- 上下文键 vs 特征键 ---------------------------------------------------- #
# 特征引擎的产出分两类，**必须分开**：
#   1. 特征（feature）：注册表里声明过的键，落 rc_feature_snapshot.features，
#      并进入模型特征向量；
#   2. 上下文（context）：求值器为支撑规则而额外附带的**原始事实**
#      （如 device_fingerprint / payload），规则可以引用它们
#      （如 payload.order_no != null），但它们不是模型输入。
# 分开的核心理由：模型离线训练用的特征列就是「注册表展开的键」，
# 若把上下文混进快照与向量，训练与推理的列集合会随事件类型漂移，
# 从而让「同一模型对不同事件类型考不同的卷」。
# 注：名单标记（subject_blacklist / subject_gray_flag 等）虽不在注册表里，
# 但它们是**正式特征**，其 FeatureSpec 声明在 list_service.FLAG_FEATURE_KEYS，
# 由 app/models/__init__.py 在导入期注册（避免模块循环导入）。
NON_FEATURE_CONTEXT_KEYS: frozenset[str] = frozenset(FEATURE_SPECS_BY_KEY)


def is_feature_key(key: str) -> bool:
    """判定一个键是「特征」还是「上下文」。

    必须同时认「基名」（``user_coupon_cnt``）与「展开名」（``user_coupon_cnt_24h``）：
    注册表里存的是基名 + 窗口清单，展开发生在计算时。若只判断基名，
    所有带窗口的特征都会被误判成上下文 —— 后果是**特征快照几乎为空、
    上下文里塞满特征**，界面与模型都会读到错的东西，而且不报任何错。

    窗口后缀必须来自该声明自己声明的 ``windows``，不能用一个全局后缀集合：
    策略师在 sys_config 里新增「30d」档位时，全局集合不会自动包含它，
    于是 ``xxx_30d`` 会被静默归入上下文。

    注意这里不做缓存：名单标记（flag）在 app.models 导入期才回填进注册表，
    任何"导入时算好的集合"都会漏掉它们。逐键线性扫描 35 条声明的开销在微秒级，
    相对一次决策的网络往返可以忽略。
    """
    if key in FEATURE_SPECS_BY_KEY:
        return True
    for spec in FEATURE_REGISTRY:
        if not spec.windows:
            continue
        prefix = f"{spec.key}_"
        if key.startswith(prefix) and key[len(prefix):] in spec.windows:
            return True
    return False


def split_context(features: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """把扁平字典拆成 (特征快照, 未声明的上下文)。

    返回顺序刻意是「快照在前」：调用点最常见的写法是
    ``snapshot, context = split_context(features)``。
    """
    snapshot: dict[str, Any] = {}
    context: dict[str, Any] = {}
    for key, value in features.items():
        (snapshot if is_feature_key(key) else context)[key] = value
    return snapshot, context


def registry_keys(windows: dict[str, int] | None = None) -> list[str]:
    """展开注册表，返回当前档位下的全部特征键。

    用途：
        1. 规则表达式的字段白名单（未知字段在保存规则时就报错）；
        2. 模型训练的特征顺序基准（训练与推理必须同序）。
    """
    active = windows if windows is not None else DEFAULT_WINDOWS
    keys: list[str] = []
    for spec in FEATURE_REGISTRY:
        keys.extend(spec.keys(active))
    return sorted(set(keys))


def distinct_subject_entities() -> dict[str, str]:
    """聚簇实体 -> 主体前缀（如 ``{"device": "user"}``）。

    由注册表推导而不是各处硬编码：写入侧（event_gateway）与读取侧
    （``_compute_distinct_features``）都依赖这份映射，
    分头维护必然漂移 —— 而漂移的表现是"聚集度特征恒为 0"，不报错。
    """
    return {
        spec.entity: spec.subject_prefix
        for spec in FEATURE_REGISTRY
        if spec.agg == "distinct" and spec.entity and spec.subject_prefix
    }


def _active_windows(db: Session | None) -> dict[str, int]:
    """当前生效的窗口档位：优先读 sys_config，读不到用默认档位。"""
    if db is None:
        return dict(DEFAULT_WINDOWS)
    try:
        raw = get_value(db, "feature_windows")
    except Exception:  # noqa: BLE001 - 配置不可用时退回默认，保证决策链不断
        return dict(DEFAULT_WINDOWS)
    if isinstance(raw, dict) and raw:
        try:
            return {str(k): int(v) for k, v in raw.items()}
        except (TypeError, ValueError):
            return dict(DEFAULT_WINDOWS)
    return dict(DEFAULT_WINDOWS)


def active_windows(db: Session | None) -> dict[str, int]:
    """对外暴露的窗口档位读取入口。

    网关与脚本需要知道「这次决策用的是哪档窗口」才能把 window_profile 写对。
    下划线版本是内部实现细节，跨模块直接用会形成隐性耦合，
    因此提供这个公开别名而不是让调用方去访问私有函数。
    """
    return _active_windows(db)


def known_feature_keys(db: Session | None = None) -> set[str]:
    """规则校验用的字段白名单。

    刻意包含**尚未实现口径**的特征（如 subject_case_cnt）：规则可以先写好、
    等数据就绪后自动生效，而不会因为"字段未注册"被拒。
    代价是这类规则在数据缺失期恒不命中 —— 由 missing_fields 留痕暴露，
    属于可接受的策略前置。

    同时并入**名单服务产出的标记键**（subject_blacklist / subject_gray_flag 等）：
    它们不是窗口聚合，因此不在本模块的注册表里，但规则确实可以引用
    （例如"用户灰名单加成"规则）。白名单必须覆盖"规则能引用的全部字段"，
    否则合法的名单类规则会在保存时被误拒。
    """
    return set(registry_keys(_active_windows(db))) | set(FLAG_FEATURE_KEYS)


@dataclass
class FeatureResult:
    """特征计算结果。"""

    features: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    cost_ms: int = 0
    feature_version: str = FEATURE_VERSION

    def get(self, key: str, default: Any = None) -> Any:
        return self.features.get(key, default)


def compute(
    db: Session,
    *,
    event_type: str,
    user_id: str,
    device_id: str | None = None,
    ip: str | None = None,
    address_hash: str | None = None,
    phone: str | None = None,
    payload: dict[str, Any] | None = None,
    occurred_at: Any = None,
    list_flags: dict[str, Any] | None = None,
) -> FeatureResult:
    """计算一次决策所需的全部特征。

    ``occurred_at`` 必须是事件自身时间：窗口边界以它为准（见模块文档）。
    ``list_flags`` 由名单服务先行计算后传入（``subject_blacklist`` 等标记类特征）。
    """
    started = time.perf_counter()
    payload = payload or {}
    now = occurred_at or utcnow()
    ts_ms = to_ms(now)

    windows = _active_windows(db)
    result = FeatureResult()

    entities: dict[str, str | None] = {
        window_store.ENTITY_USER: user_id,
        window_store.ENTITY_DEVICE: device_id,
        window_store.ENTITY_IP: ip,
        window_store.ENTITY_ADDRESS: address_hash,
        window_store.ENTITY_PHONE: phone,
    }
    # 聚簇特征需要知道"当前事件的主体是谁"，才能把当前事件也算进去重集合
    # （事件在决策前尚未写入条带，见 _compute_distinct 注释）。
    subject_of_entity = {
        window_store.ENTITY_DEVICE: f"user:{user_id}",
        window_store.ENTITY_IP: f"user:{user_id}",
        window_store.ENTITY_ADDRESS: f"user:{user_id}",
    }

    for spec in FEATURE_REGISTRY:
        if spec.agg == "count":
            _compute_count(result, spec, entities, windows, ts_ms)
        elif spec.agg == "sum":
            _compute_sum(result, spec, entities, windows, ts_ms, payload)

    _compute_distinct_features(result, entities, windows, ts_ms, subject_of_entity)
    _compute_current_event_counts(
        result,
        entities=entities,
        event_type=event_type,
        windows=windows,
    )
    _compute_business_features(
        result,
        db,
        payload=payload,
        user_id=user_id,
        address_hash=address_hash,
        event_type=event_type,
        now=now,
    )
    _compute_derived(result, windows)
    _apply_feature_defaults(result)
    _apply_list_flags(result, list_flags or {})

    result.cost_ms = int((time.perf_counter() - started) * 1000)
    return result


# --------------------------------------------------------------------------- #
# 窗口聚合实现
# --------------------------------------------------------------------------- #
def _mark_missing(result: FeatureResult, key: str) -> None:
    """登记缺失特征（去重）。"""
    if key not in result.missing_fields:
        result.missing_fields.append(key)


def _compute_count(result: FeatureResult, spec: FeatureSpec, entities, windows, ts_ms: int) -> None:
    for window in spec.windows:
        if window not in windows:
            continue
        entity_id = entities.get(spec.entity or "")
        key = f"{spec.key}_{window}"
        if not entity_id:
            # 事件没带该实体（如未提供收货地址）：整组记 0 并标缺失。
            # 0 与"缺失"并存不矛盾：0 让规则能算出结果，
            # missing_fields 让审核员知道这个 0 是"真的没有"还是"没数据"。
            result.features[key] = 0
            _mark_missing(result, key)
            continue
        total = 0
        for event_type in spec.event_types:
            total += window_store.safe_call(
                0,
                window_store.count,
                entity=spec.entity,
                entity_id=entity_id,
                event_type=event_type,
                window=window,
                window_seconds=windows[window],
                now_ms=ts_ms,
            )
        result.features[key] = total


def _compute_sum(result: FeatureResult, spec: FeatureSpec, entities, windows, ts_ms: int, payload) -> None:
    for window in spec.windows:
        if window not in windows:
            continue
        entity_id = entities.get(spec.entity or "")
        key = f"{spec.key}_{window}"
        if not entity_id:
            result.features[key] = 0.0
            _mark_missing(result, key)
            continue
        total = 0.0
        for event_type in spec.event_types:
            total += window_store.safe_call(
                0.0,
                window_store.sum_amount,
                entity=spec.entity,
                entity_id=entity_id,
                event_type=event_type,
                window=window,
                window_seconds=windows[window],
                now_ms=ts_ms,
            )
        # 加上当前事件自身的金额：事件在决策后才入条带，不加的话
        # 第一条领券事件的特征会是 0（与"1h 内领券 1 次、金额 20"的直觉不符），
        # 也会让"单笔大额"类规则在首单失效。
        total += _payload_amount(payload, spec.amount_field)
        result.features[key] = round(total, 2)


def _compute_current_event_counts(
    result: FeatureResult,
    *,
    entities: dict[str, str | None],
    event_type: str,
    windows: dict[str, int],
) -> None:
    """把「当前这条事件」计入**它自己涉及的每一个维度**的频次特征。

    条带写入发生在决策**之后**（见 event_gateway 的模块文档），
    所以特征计算时当前事件在任何维度上都还不在 ZSET 里。不补偿的后果是
    每条链路的**第一次动作**频次恒为 0："1 小时内领券 1 次"算成 0，
    而"首单/首次领券"恰恰是最该被风控关注的行为 —— 且全程不报任何错。

    补偿范围必须覆盖**全部维度**（user / device / ip / address / phone），
    不能只补 user 维度：device 上的计数说的是"这台设备上发生过几次领券"，
    本次事件同样是那次动作，漏补会让设备维度的频次永远比真实值少 1，
    于是 ``device_coupon_cnt_1h > 5`` 这类规则永远差一次才命中。

    两个条件缺一不可：
      1. ``spec.agg == "count"``：求和类由 ``_compute_sum`` 自行加上 payload 金额
         （同样的理由，同样的必要性）；ratio 派生自计数，会自动跟着对；
      2. 当前事件类型在该声明的 ``event_types`` 里，且该维度取值存在 ——
         维度缺失时特征已记 0 并进 missing_fields，不该再补成 1。
    """
    for spec in FEATURE_REGISTRY:
        if spec.agg != "count" or event_type not in spec.event_types:
            continue
        if not entities.get(spec.entity or ""):
            continue
        for key in spec.keys(windows):
            current = result.features.get(key)
            if isinstance(current, (int, float)):
                result.features[key] = current + 1


def _compute_distinct_features(result: FeatureResult, entities, windows, ts_ms: int, subject_of_entity) -> None:
    """聚簇特征：主体去重。

    **跨事件类型的去重必须由本函数合并，不能各键各算**：
    ``device_account_cnt_24h`` 覆盖 login / coupon_receive / order_create / order_pay
    四类事件，同一个账号可能既有登录又有领券。若把每类事件的去重结果相加，
    这个账号会被数两次（"同设备关联 3 个账号"变成"6 个"），阈值静默失效。
    因此这里把所有事件类型的成员**一次取出后合并去重**，
    再补上"当前事件的主体"（事件尚未入条带）。
    """
    for spec in FEATURE_REGISTRY:
        if spec.agg != "distinct":
            continue
        for window in spec.windows:
            if window not in windows:
                continue
            entity_id = entities.get(spec.entity or "")
            key = f"{spec.key}_{window}"
            if not entity_id:
                result.features[key] = 0
                _mark_missing(result, key)
                continue
            subjects = window_store.safe_call(
                set(),
                window_store.distinct_subjects,
                entity=spec.entity,
                entity_id=entity_id,
                event_types=list(spec.event_types),
                window=window,
                window_seconds=windows[window],
                now_ms=ts_ms,
                prefix=spec.subject_prefix,
            )
            current_subject = subject_of_entity.get(spec.entity or "")
            if current_subject and current_subject.startswith(f"{spec.subject_prefix}:"):
                subjects = set(subjects) | {current_subject.split(":", 1)[1]}
            result.features[key] = len(subjects)


def _payload_amount(payload: dict[str, Any], field_name: str | None) -> float:
    """从 payload 安全取金额：取不到或格式不对都记 0（不抛错）。"""
    if not field_name:
        return 0.0
    raw = payload.get(field_name)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# 业务表 / 画像类特征
# --------------------------------------------------------------------------- #
def _compute_business_features(
    result: FeatureResult,
    db: Session,
    *,
    payload: dict[str, Any],
    user_id: str,
    address_hash: str | None,
    event_type: str,
    now,
) -> None:
    """来自业务表与固定口径计算的画像特征。"""
    # ---- 账号注册天数 ----
    customer = db.execute(select(BizCustomer).where(BizCustomer.user_id == user_id)).scalar_one_or_none()
    if customer is None:
        result.features["account_age_days"] = None
        _mark_missing(result, "account_age_days")
    else:
        result.features["account_age_days"] = max(int((now - customer.register_at).total_seconds() // 86400), 0)

    # ---- 环境指纹风险分：固定权重累加，上限 100 ----
    # 用固定权重而非模型：这一项必须能向审核员一句话解释清楚。
    result.features["device_env_risk"] = _env_risk_score(payload.get("fingerprint"))

    # ---- IP 代理/数据中心 ----
    result.features["ip_is_datacenter"] = bool(payload.get("is_proxy"))

    # ---- IP 归属地与收货地一致性 ----
    ip_region = payload.get("ip_region")
    address_region = payload.get("address_region")
    if ip_region and address_region:
        result.features["ip_region_mismatch"] = ip_region != address_region
    else:
        result.features["ip_region_mismatch"] = None
        _mark_missing(result, "ip_region_mismatch")

    # ---- 夜间行为（P0 简化口径见注册表 description）----
    hour_cn = (now.hour + 8) % 24
    result.features["night_activity_ratio"] = 1.0 if 0 <= hour_cn < 6 else 0.0

    # ---- 首单即退款 ----
    result.features["user_first_order_refund"] = _is_first_order_refund(db, user_id=user_id, event_type=event_type)

    # ---- 同收货地址关联账号数 ----
    if address_hash:
        cnt = db.execute(
            select(func.count(func.distinct(BizOrder.user_id))).where(BizOrder.address_hash == address_hash)
        ).scalar_one()
        result.features["address_phone_share_cnt"] = int(cnt or 0)
    else:
        result.features["address_phone_share_cnt"] = 0
        _mark_missing(result, "address_phone_share_cnt")

    # ---- 同设备新账号数（device_new_account_ratio 的分子）----
    # P0 口径：账号注册天数 < 7 天记为新账号；设备维度的历史账号集合不落库，
    # 因此用"当前账号是否新账号"近似（0/1），并在交付说明中标注为已知简化。
    result.features["device_new_account_cnt"] = 1 if (result.features.get("account_age_days") or 0) < 7 else 0

    # ---- 近 30 天案件数：案件表属 P1，P0 固定 0 ----
    result.features["subject_case_cnt"] = 0
    _mark_missing(result, "subject_case_cnt")


def _env_risk_score(fingerprint: Any) -> int:
    """环境指纹风险分（0-100）：可疑信号固定权重累加。"""
    if not isinstance(fingerprint, dict):
        return 0
    score = 0
    if fingerprint.get("is_emulator"):
        score += 40
    if fingerprint.get("is_rooted"):
        score += 25
    if fingerprint.get("is_multi_app"):
        score += 20
    if fingerprint.get("is_virtual_location"):
        score += 15
    screen = fingerprint.get("screen")
    if isinstance(screen, str) and screen in {"0x0", "1x1", "unknown"}:
        score += 10
    return min(score, 100)


def _is_first_order_refund(db: Session, *, user_id: str, event_type: str) -> bool:
    """首单即退款：当前是退款申请，且该用户历史订单数不超过 1。"""
    if event_type != EVENT_AFTER_SALE_APPLY:
        return False
    order_cnt = db.execute(select(func.count(BizOrder.id)).where(BizOrder.user_id == user_id)).scalar_one()
    return int(order_cnt or 0) <= 1


def _compute_derived(result: FeatureResult, windows: dict[str, int]) -> None:
    """第二遍计算：ratio 类特征（依赖第一遍的计数结果）。"""
    for spec in FEATURE_REGISTRY:
        if spec.agg != "ratio":
            continue
        for window in spec.windows:
            if window not in windows:
                continue
            numerator = _lookup_for_ratio(result, spec.numerator, window)
            denominator = _lookup_for_ratio(result, spec.denominator, window)
            key = f"{spec.key}_{window}"
            if not isinstance(numerator, (int, float)) or not isinstance(denominator, (int, float)):
                result.features[key] = 0.0
                _mark_missing(result, key)
                continue
            # 分母为 0 时取 0 而非报错：新账号没有订单是常态，
            # "退款率"在这种情形下无意义，取 0 让规则自然不命中。
            result.features[key] = round(numerator / denominator, 4) if denominator else 0.0


def _lookup_for_ratio(result: FeatureResult, base: str | None, window: str) -> Any:
    """为 ratio 特征查找分子/分母的值。

    先找带窗口的键（``user_refund_cnt_24h``），找不到再找无窗口的基名
    （``device_new_account_cnt`` 这类画像特征）——
    没有这个回退，``device_new_account_ratio`` 会因为分子键名带窗口而恒取 0。
    """
    if not base:
        return None
    keyed = result.features.get(f"{base}_{window}")
    return result.features.get(base) if keyed is None else keyed


def _apply_feature_defaults(result: FeatureResult) -> None:
    """给标记类（flag）特征填默认值。

    名单类特征由 ``list_service`` 计算后覆盖，但"本次没命中任何名单"
    与"名单服务没跑"是两件事：前者应为 False，后者应为缺失。
    这里统一先填 False，再由名单结果覆盖 —— 规则因此总能拿到确定的布尔值。
    """
    for spec in FEATURE_REGISTRY:
        if spec.agg == "flag":
            result.features.setdefault(spec.key, False)


def _apply_list_flags(result: FeatureResult, list_flags: dict[str, Any]) -> None:
    """把名单服务算出的标记合并进特征字典。"""
    for key, value in list_flags.items():
        result.features[key] = value
