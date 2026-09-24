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

# "半可疑"指纹：**合法但在环境上确实有点像作弊**的用户。
#
# 现实里每个平台都有一小撮这样的人：爱 Root 玩机的发烧友、用云手机挂游戏的人、
# 装了改定位插件被误判的人、以及企业内部测试机。他们的 ``device_env_risk``
# 落在 15~55 分之间 —— "可疑但远远不足以判定"。
#
# **没有这部分样本的后果**：环境风险分变成"非 0 即作弊"的一刀切特征。
# 任何以环境分划定阈值的策略都会在真实环境里打出成片误拦（风控里最贵的一类错误），
# 而模型学到的"环境可疑 ⇒ 作弊"在数据集上完美成立、上线当天就崩 ——
# 这是"合成数据太干净"最典型的翻车方式。
RISKY_NORMAL_FINGERPRINTS: tuple[dict[str, Any], ...] = (
    # 25 分：Root 过的机器
    {"os": "Android", "model": "Xiaomi 14", "screen": "1200x2670", "app_version": "8.2.0", "is_rooted": True},
    # 15 分：装了改定位插件
    {"os": "Android", "model": "OnePlus Ace 2", "screen": "1240x2772", "app_version": "8.2.0", "is_virtual_location": True},
    # 10 分：定制 ROM，分辨率上报异常
    {"os": "Android", "model": "Redmi Note 13", "screen": "1x1", "app_version": "8.1.6"},
    # 30 分：多开 App（工作号 + 生活号）
    {"os": "Android", "model": "HUAWEI Mate 60", "screen": "1216x2688", "app_version": "8.2.0", "is_multi_app": True},
    # 55 分：云手机/模拟器用户（挂游戏、跑脚本但**不是**黑产）
    {"os": "Android", "model": "CloudPhone-3", "screen": "1080x1920", "app_version": "8.2.0", "is_emulator": True, "is_virtual_location": True},
    # 65 分：云手机 + 改定位 + 异常分辨率（工作室用云机做正经营生的也不少）
    {
        "os": "Android",
        "model": "CloudPhone-7",
        "screen": "0x0",
        "app_version": "8.2.0",
        "is_emulator": True,
        "is_virtual_location": True,
    },
    # 70 分：模拟器 + 多开 + 异常分辨率（自动化测试机）
    #
    # 这个分值**与设备农场的最低分（70）重合**，是有意为之：
    # 云手机/模拟器并不是黑产的专利，测试机与云游戏用户也会长这样。
    # 环境分只能作为"需要进一步核实"的信号，不能单独定案 ——
    # 这正是 PRD 里环境类规则给 25~40 分（而非直接 Reject）的原因。
    {"os": "Android", "model": "Pixel 6 API 33", "screen": "0x0", "app_version": "8.2.0", "is_emulator": True, "is_multi_app": True},
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
#
# 代理池**不能只有两三个出口**：实测农场账号共用 3 个代理 IP 时，
# ``ip_account_cnt_24h`` 单特征 AUC 就达到 0.89 —— 模型学到的其实是
# 「这个 IP 在作弊 IP 名单里」，而不是任何行为规律（真实代理池
# 每天轮换上千个出口，同一个 IP 上不会长期挂着同一个团伙）。
# 这里给到 12 个出口，让该特征只保留"这些账号在换 IP"的弱信号。
PROXY_IPS: tuple[str, ...] = (
    "103.45.201.17",
    "45.192.88.4",
    "185.220.101.9",
    "103.75.190.6",
    "45.132.75.28",
    "185.234.72.11",
    "91.219.236.44",
    "104.168.99.71",
    "45.155.205.233",
    "193.32.126.8",
    "146.70.121.55",
    "89.187.168.3",
)

# 合法用户也会出现的"数据中心 IP"：企业专线出口、云桌面、机场 VPN、云游戏。
#
# **没有这部分样本的后果与 RISKY_NORMAL_FINGERPRINTS 完全一样**：
# ``ip_is_datacenter`` 会变成"命中即作弊"的一刀切特征（实测该特征在
# 正常用户上恒为 0、在农场上恒为 1）。真实环境里 VPN 用户占比不低，
# 而"用 VPN 就拦"这件事没有任何业务依据 —— 它只说明用户的网络出口
# 不在家宽段里，谈不上风险。
LEGIT_DATACENTER_IPS: tuple[str, ...] = (
    "203.0.113.24",
    "198.51.100.77",
    "203.0.113.161",
    "198.51.100.9",
    "192.0.2.51",
    "192.0.2.133",
)

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
    risky_env: bool = False,
    vpn: bool = False,
    home_ip: bool = False,
) -> Actor:
    """构造一个主体。

    ``device_pool`` 传入时，主体从池中**随机挑一台**设备 —— 这正是"设备农场"的
    本质（多账号共用少量设备），也是 ``device_account_cnt_24h`` 能识别出的模式。

    ``risky_env=True`` 用于生成"环境半可疑的合法用户"：指纹取自
    ``RISKY_NORMAL_FINGERPRINTS``（Root、云手机、多开、改定位），其余一切正常。
    它存在的意义是让环境风险分**不再是一个二值标签**（见该常量的说明）。

    ``vpn=True`` 用于生成"走 VPN/企业出口的合法用户"：``is_proxy`` 为真、
    IP 归属地与收货地不一致。理由同 ``risky_env``（见 ``LEGIT_DATACENTER_IPS``）。

    ``home_ip=True`` 用于农场账号的**异质性**：一部分农场账号不用代理，
    直接挂着家宽下单。真实农场会按成本与风控强度轮换出口，
    若整个农场清一色代理 IP，``ip_is_datacenter`` 就等价于"是农场"。

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
        if home_ip:
            # 一部分农场账号直接挂家宽出口：IP 不与收货地冲突、也不是代理段。
            # 这样一来，"代理 IP + 归属地不符"只能覆盖农场的一部分账号，
            # 环境类特征退化为弱信号（这正是线上真实的样子）。
            region = rng.choice(REGIONS)
            network = Network(
                ip=f"121.40.{(index % 200) + 10}.{rng.randint(2, 250)}",
                ip_region=region,
                is_proxy=False,
            )
            address = Address(address_hash=address_hash or f"ADDR-{index:06d}", region=region)
        else:
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

    fingerprint_pool = RISKY_NORMAL_FINGERPRINTS if risky_env else NORMAL_FINGERPRINTS
    device = Device(device_id=f"D-{index:06d}", fingerprint=dict(rng.choice(fingerprint_pool)))
    region = rng.choice(REGIONS)
    if vpn:
        return Actor(
            user_id=user_id,
            phone=phone,
            device=device,
            # 出口在城市/云机房，与本人的收货地天然不一致 —— 这类"不一致"
            # 在真实数据里绝大多数来自公司网络与 VPN，而不是作案。
            network=Network(ip=rng.choice(LEGIT_DATACENTER_IPS), ip_region="CN-HK", is_proxy=True),
            address=Address(address_hash=address_hash or f"ADDR-{index:06d}", region=region),
            profile="normal_vpn",
            tags=["数据中心 IP", "正常用户"],
        )
    return Actor(
        user_id=user_id,
        phone=phone,
        device=device,
        network=Network(ip=f"112.65.{index % 250}.{rng.randint(2, 250)}", ip_region=region, is_proxy=False),
        address=Address(address_hash=address_hash or f"ADDR-{index:06d}", region=region),
        profile=profile,
        tags=["半可疑环境", "正常用户"] if risky_env else [],
    )


def build_device_pool(size: int = 5, *, prefix: str = "D-FARM") -> list[Device]:
    """构造设备池（农场共用的少量设备指纹）。"""
    return [
        Device(device_id=f"{prefix}-{index}", fingerprint=dict(FARM_FINGERPRINTS[index % len(FARM_FINGERPRINTS)]))
        for index in range(size)
    ]


def build_shared_home_actor(
    rng: random.Random,
    *,
    index: int,
    shared_device: Device,
    shared_address: str,
) -> Actor:
    """构造一个**完全正常、但环境与作弊画像重叠**的用户。

    典型场景：三代同堂共用一台手机与一个收货地址，或合租房里几个同事共用
    公司出口 IP。这类用户的 ``device_account_cnt`` 与
    ``address_account_cnt`` 会跟团伙欺诈一样高，环境特征无法区分。

    **它的存在是为了让数据集有判别难度**：没有这类样本时，模型会学到
    "环境可疑 = 作弊"，AUC 冲到 1.0 —— 而这条规律一旦上线，
    共享设备的正常家庭与公司网络会被成片误拦，是风控里代价最高的一类错误。
    加了它之后，模型必须依靠行为序列（频次、金额、退款节奏）才分得开。
    """
    region = rng.choice(REGIONS)
    return Actor(
        user_id=f"U{index:06d}",
        phone=phone_of(index),
        device=shared_device,
        network=Network(
            ip=f"112.65.{index % 250}.{rng.randint(2, 250)}", ip_region=region, is_proxy=False
        ),
        address=Address(address_hash=shared_address, region=region),
        is_cheater=False,
        profile="shared_home",
        tags=["共用设备", "共用地址", "正常用户"],
    )


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
