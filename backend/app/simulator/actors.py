"""主体生成器：账号、设备、网络、收货地址。

**一切随机都必须可复现**（PRD §14.1 要求固定随机种子）：演示与验收要能"重跑一次
得到同样的结论"，否则一次通过、一次不通过时无法判断是策略变化还是随机波动。
因此本模块不接受"全局 random"，只用调用方传入的 ``random.Random(seed)`` 实例。

作弊账号的画像刻意做得**有区分度但不夸张**：

| 画像 | 设备 | IP | 账号年龄 | 指纹 |
| --- | --- | --- | --- | --- |
| 正常用户 | 每人独立设备 | 家宽、归属地与收货地一致 | 数十天~数年 | 正常 |
| 设备农场 | 40 个账号共用 5 台设备 | 3 个代理 IP，归属地不一致 | 0~3 天 | 含模拟器/多开标记 |
| 恶意退款 | 正常设备 | 正常 IP | 数十天 | 正常 |

恶意退款账号的画像刻意"干净"：它的风险体现在**行为序列**（24h 内 3 笔高额退款、
收货地址 7 天内关联多个账号的退款），而不是环境特征。
如果把它也写成模拟器 + 代理 IP，任何一条环境规则都能拦住它，
就演示不出"规则命中但分数落在 60~79 需要人工审核"这一段。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

# 设备指纹模板（正常设备）
NORMAL_FINGERPRINTS: tuple[dict[str, Any], ...] = tuple(
    {
        "os": os_name,
        "model": model,
        "screen": screen,
        "app_version": "8.2.0",
    }
    for os_name, model, screen in (
        ("Android", "Xiaomi 14", "1200x2670"),
        ("Android", "HUAWEI Mate 60", "1216x2688"),
        ("iOS", "iPhone 15", "1179x2556"),
        ("iOS", "iPhone 13", "1170x2532"),
        ("Android", "OPPO Find X7", "1264x2780"),
    )
)

# 设备农场的指纹：模拟器 + 多开 + 异常分辨率（命中 RC_ENV_* 类规则）
FARM_FINGERPRINTS: tuple[dict[str, Any], ...] = tuple(
    {
        "os": "Android",
        "model": f"Emulator-{index}",
        "screen": "0x0",
        "app_version": "8.2.0",
        "is_emulator": True,
        "is_multi_app": True,
        "is_rooted": index % 2 == 0,
    }
    for index in range(5)
)

# 代理/数据中心 IP 段（命中 RC_ENV_004：ip_is_datacenter）
PROXY_IPS: tuple[str, ...] = ("103.45.201.17", "45.192.88.4", "185.220.101.9")

REGIONS: tuple[str, ...] = ("CN-SH", "CN-BJ", "CN-GZ", "CN-HZ")


@dataclass(frozen=True)
class Device:
    """设备（设备号 + 指纹）。"""

    device_id: str
    fingerprint: dict[str, Any]


@dataclass(frozen=True)
class Network:
    """网络环境。"""

    ip: str
    ip_region: str | None = None
    is_proxy: bool = False


@dataclass(frozen=True)
class Address:
    """收货信息。``address_hash`` 是"同一收货地址"的判定依据。"""

    address_hash: str
    region: str | None = None
    receiver_name: str | None = None
    phone: str | None = None


@dataclass
class Actor:
    """一个业务主体（账号）。"""

    user_id: str
    phone: str
    device: Device
    network: Network
    address: Address | None = None
    is_cheater: bool = False
    profile: str = "normal"
    tags: list[str] = field(default_factory=list)


def phone_of(index: int) -> str:
    """按序号生成稳定的手机号（便于人工核对，不用随机串）。"""
    return f"139{index:08d}"


def build_actor(
    rng: random.Random,
    *,
    index: int,
    profile: str = "normal",
    device_pool: list[Device] | None = None,
    address_hash: str | None = None,
    stealth: bool = False,
) -> Actor:
    """构造一个主体。

    ``device_pool`` 传入时，主体从池中**随机挑一台**设备 —— 这正是"设备农场"的
    本质（多账号共用少量设备），也是 ``device_account_cnt_24h`` 能识别出的模式。

    ``stealth=True`` 用于演示**规则与模型的边界**：攻击者刻意避开全部环境特征
    （独立设备、家宽 IP、指纹干净），只有"单账号短时高频领券"这一个频次信号。
    这类样本规则分只有 20，落在放行区间；它存在的意义是让演示具备说服力 ——
    否则"所有作弊流量都被 Reject"看起来像规则写得越狠越好，
    掩盖了"单一弱信号需要模型/策略加权"这个真实的工程结论。
    """
    user_id = f"U{index:06d}"
    phone = phone_of(index)

    if profile == "device_farm":
        if stealth:
            # 隐蔽型农场账号：物理设备与家宽网络，只保留高频行为
            region = rng.choice(REGIONS)
            return Actor(
                user_id=user_id,
                phone=phone,
                # 指纹必须排除 is_emulator 等标记：农场模板自带模拟器标记，
                # 用它就失去了"环境干净"的前提，对照组的结论也就没了意义
                device=Device(
                    device_id=f"D-CLEAN-{index}",
                    fingerprint=_clean_fingerprint(rng),
                ),
                network=Network(
                    ip=f"112.65.{(index % 200) + 10}.{rng.randint(2, 250)}", ip_region=region, is_proxy=False
                ),
                address=Address(address_hash=address_hash or f"ADDR-{index:06d}", region=region),
                is_cheater=True,
                profile="stealth_farm",
                tags=["隐蔽薅羊毛", "仅频次异常"],
            )
        # 设备分配用**轮询**而不是随机抽：随机抽会让"每台设备上挂了几个账号"变成
        # 概率事件 —— 账号数少时可能整场都没有一台设备达到 device_account_cnt_24h >= 3，
        # "同设备多账号"这条识别路径就时灵时不灵，演示与回归测试都无法稳定复现。
        # 轮询既贴合农场运营者的真实做法（按顺序把账号铺到设备上），也让聚类可预期。
        device = (
            device_pool[index % len(device_pool)]
            if device_pool
            else Device(device_id=f"D-FARM-{index % 5}", fingerprint=dict(rng.choice(FARM_FINGERPRINTS)))
        )
        network = Network(ip=rng.choice(PROXY_IPS), ip_region="CN-HK", is_proxy=True)
        # 收货地写成与 IP 归属地不同的省份：命中 RC_ENV_005（IP 与收货地不一致）
        address = Address(address_hash=address_hash or f"ADDR-{index:06d}", region="CN-SH")
        return Actor(
            user_id=user_id,
            phone=phone,
            device=device,
            network=network,
            address=address,
            is_cheater=True,
            profile=profile,
            tags=["设备农场", "代理 IP", "新账号"],
        )

    if profile == "refund_fraud":
        device = Device(device_id=f"D-{index:06d}", fingerprint=dict(rng.choice(NORMAL_FINGERPRINTS)))
        region = rng.choice(REGIONS)
        return Actor(
            user_id=user_id,
            phone=phone,
            device=device,
            network=Network(ip=f"112.65.{index % 250}.{rng.randint(2, 250)}", ip_region=region, is_proxy=False),
            # 共用收货地址：多账号共享同一地址是"地址维度聚集"的信号
            address=Address(address_hash=address_hash or f"ADDR-{index:06d}", region=region),
            is_cheater=True,
            profile=profile,
            tags=["高额退款", "地址聚集"],
        )

    device = Device(device_id=f"D-{index:06d}", fingerprint=dict(rng.choice(NORMAL_FINGERPRINTS)))
    region = rng.choice(REGIONS)
    return Actor(
        user_id=user_id,
        phone=phone,
        device=device,
        network=Network(ip=f"112.65.{index % 250}.{rng.randint(2, 250)}", ip_region=region, is_proxy=False),
        address=Address(address_hash=address_hash or f"ADDR-{index:06d}", region=region),
        profile=profile,
    )


def build_device_pool(size: int = 5, *, prefix: str = "D-FARM") -> list[Device]:
    """构造设备池（农场共用的少量设备指纹）。"""
    return [
        Device(device_id=f"{prefix}-{index}", fingerprint=dict(FARM_FINGERPRINTS[index % len(FARM_FINGERPRINTS)]))
        for index in range(size)
    ]


def _clean_fingerprint(rng: random.Random) -> dict[str, Any]:
    """挑一个**不含任何可疑标记**的指纹（用于隐蔽型对照组）。

    显式清掉 is_emulator / is_rooted / is_multi_app / is_virtual_location：
    这些键一旦存在就会被 ``_env_risk_score`` 累加，导致对照组不再"干净"。
    这里不依赖模板"恰好没有这些键"，而是主动剔除 —— 模板将来新增标记时，
    对照组不会悄悄失效。
    """
    fingerprint = dict(rng.choice(NORMAL_FINGERPRINTS))
    for marker in ("is_emulator", "is_rooted", "is_multi_app", "is_virtual_location"):
        fingerprint.pop(marker, None)
    return fingerprint
