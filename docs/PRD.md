# 电商风险控制系统 — 产品需求文档（PRD）

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.0 |
| 编写日期 | 2026-09-23 |
| 需求来源 | `项目实战.md` §2.3（电商风险控制系统） |
| 关联文档 | `AGENTS.md`（协作规范）、`docs/DESIGN.md`（UI 设计规范） |
| 文档状态 | 已评审（经设计树访谈逐项确认） |
| 交付形态 | 单仓库：`backend/`（FastAPI）+ `frontend/`（React）+ `scripts/` + `docs/` |

---

## 0. 分期标记说明

本文档描述**全系统**需求，所有条目均带分期标记，作为分阶段交付与验收的依据：

| 标记 | 含义 | 交付内容 |
| --- | --- | --- |
| **P0** | 决策底座（无界面） | 数据模型 + 迁移 + 种子 + 模拟业务端 + 事件接入网关 + 特征引擎 + 名单 + 规则引擎 + 模型引擎 + 融合仲裁 + 落库 + 幂等 + 审计链 |
| **P1** | 案件闭环 | 案件流转（合案、状态机）+ 处置联动 + 审核工作台页面 |
| **P2** | 运营与配置界面 | 态势大盘（含 SSE）+ 策略与规则配置页 + 事件仿真页 + 审计与模型页 + 监控预聚合 |
| **P3** | 演示与验收 | 全量数据集 + 两个作弊对抗场景脚本 + 脚本化验收清单 + README/演示手册 |

**MVP 边界**：**P0 + P1** 即构成"最小可用闭环"（事件进得来、判得准、案子出得来、处置落得下）。P2 提供运营视角，P3 提供可复现的演示与答辩材料。

---

## 1. 背景与目标

电商业务在营销、交易、售后三个环节持续面临黑产攻击：羊毛党用批量账号刷券、黄牛用设备农场囤货、虚假交易刷单、恶意退款索赔。传统做法是把风控逻辑硬编码在业务代码里，导致策略无法迭代、证据无法沉淀、处置无法追溯。

本项目构建一套**实时风险控制系统**，把风控从业务代码中剥离为独立服务，实现：

1. **实时判定**：业务动作发生时同步返回放行 / 挑战 / 人审 / 拦截结论。
2. **双引擎决策**：可解释的规则引擎（人工策略）+ 可复现的模型引擎（离线训练、在线推理）融合打分。
3. **链路闭环**：高危事件自动沉淀为案件，人工复核后一键处置并真实写回业务单据。
4. **全程留痕**：所有决策、策略变更、人工处置记入不可篡改的哈希链审计日志。

**交付目标**：一套可本地运行、可现场演示、可复现验收的端到端系统，覆盖两个典型作弊对抗场景。

---

## 2. 术语表

| 术语 | 含义 |
| --- | --- |
| 事件（Event） | 业务系统发起的一次待风控的业务动作，共五类（见 §6.1） |
| 主体（Subject） | 风险归因对象，本系统以业务用户 `user_id` 为主，辅以设备 / IP / 手机号 / 地址 |
| 特征快照（Feature Snapshot） | 事件发生瞬间计算出的多维特征键值集合 |
| 规则分（rule_score） | 命中规则分值累加结果，0~100 |
| 模型分（model_score） | 模型引擎对该事件输出的风险分，0~100 |
| 综合分（risk_score） | 规则分与模型分融合后的最终分，决定风险等级与决策动作 |
| 名单（List） | 黑 / 白 / 灰三类名单，维度为用户 / 手机号 / IP / 设备 / 地址 |
| 案件（Case） | 中高风险事件沉淀出的人工调查工单 |
| 处置（Disposal） | 审核员对案件作出的业务结论 + 风控动作 |
| 合案（Case Merge） | 同一主体同一场景在窗口期内多次触发时合并为一个案件 |
| 动作提示（action_hint） | 规则上声明的处置倾向，目前仅用于声明 Challenge |
| 仿真事件 | 由仿真页或测试脚本注入的事件，`source=simulation`，默认不计入大盘 |

---

## 3. 范围

### 3.1 范围内

- 五类业务事件的接入、校验、幂等与分发。
- 基于 Redis 滑动窗口的实时特征计算与快照持久化。
- 名单过滤 + 规则引擎 + 模型引擎 + 融合仲裁的四段式决策流水线。
- 案件自动生成、合案、状态机流转、人工复核与处置联动。
- 规则与名单的全生命周期管理（增删改查、启停、版本留痕）。
- 实体关联图谱（同设备 / 同 IP / 同地址 / 同手机号聚集）与用户画像。
- 态势大盘（实时指标、趋势、分布、排行、挽损）、SSE 实时推送。
- 事件仿真测试（单条注入 + 判定链路单步回溯）。
- 哈希链审计日志与链完整性校验、模型版本管理。

### 3.2 非目标（明确不做）

| 不做项 | 原因 |
| --- | --- |
| 对接真实电商系统与支付通道 | 交付要求为可运行演示链路，用内置模拟业务端替代 |
| 容器化、集群、高可用、限流压测 | 演示级交付，不作运维工程化投入 |
| 模型训练管线服务化（MLflow / 独立推理服务） | 采用离线脚本 + 模型文件在线推理，收益成本比更高 |
| 前端组件测试与 Playwright E2E | 验证资源集中在决策链路（后端单测 + 集成测试） |
| 定时归档任务 | 归档由管理员显式操作触发 |
| Redis 之外的消息中间件（Kafka/RocketMQ） | 事件流由内置生成器与模拟业务端驱动，无需引入 |

---

## 4. 角色与权限

### 4.1 角色定义

| 角色 | 代码 | 职责 |
| --- | --- | --- |
| 风控审核员 | `auditor` | 查看待审案件、核对证据链与关联图谱、录入复核结论并执行业务处置 |
| 风控策略师 | `strategist` | 配置规则集与权重阈值、维护多维名单库、执行策略回测与仿真、管理模型版本 |
| 系统管理员 | `admin` | 系统运行配置、监控吞吐与接口健康度、查阅审计流水与校验审计链 |

### 4.2 权限矩阵

| 资源 / 操作 | auditor | strategist | admin |
| --- | --- | --- | --- |
| 态势大盘 | 读 | 读 | 读 |
| 事件仿真 | 读 | 读写 | 读写 |
| 决策明细 | 读 | 读 | 读 |
| 案件列表 / 详情 | 读写 | 只读 | 读写 |
| 案件接手 / 处置 / 归档 | 执行 | — | 执行 |
| 规则增删改 / 启停 | 只读 | 读写 | 读写 |
| 名单库维护 / 批量导入 | 只读 | 读写 | 读写 |
| 模型版本查看 / 切换 | — | 读写 | 读写 |
| 审计流水 / 链校验 | — | — | 读写 |
| 系统配置（阈值 / α / 窗口） | — | 读写 | 读写 |

权限在**后端接口级**强制校验（FastAPI 依赖注入），前端仅做菜单与按钮的显隐配合，不作为安全边界。

### 4.3 预置账号

| 用户名 | 密码 | 角色 |
| --- | --- | --- |
| `admin` | `admin123` | 系统管理员 |
| `strategist` | `strategy123` | 风控策略师 |
| `auditor` | `audit123` | 风控审核员 |

密码以 bcrypt 摘要存储；登录签发 JWT（HS256，有效期 7 天）。

---

## 5. 系统架构

### 5.1 运行形态与端口

| 组件 | 地址 | 说明 |
| --- | --- | --- |
| 前端（Vite dev server） | `http://127.0.0.1:5173` | 开发态，`/api` 反向代理到后端 8000 |
| 后端（Uvicorn + FastAPI） | `http://127.0.0.1:8000` | 提供 REST + SSE |
| 单端口演示模式 | `http://127.0.0.1:8000` | 前端 `build` 产物由 FastAPI 静态托管，供答辩演示 |
| MySQL | `127.0.0.1:3306`，库 `risk_control` | 账号 `root` / `123456`（本地托管） |
| Redis | `127.0.0.1:6379` | 本地托管 |

一键启动脚本 `scripts/dev.ps1` 同时拉起后端与前端；数据库初始化 `scripts/init_db.ps1`（建库 → Alembic 迁移 → 种子数据）。

### 5.2 技术栈

| 层 | 选型 |
| --- | --- |
| 前端 | React 18 + TypeScript + Vite + Ant Design 5 + ECharts 5 + React Router + TanStack Query |
| 后端 | Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2.0 + Alembic + PyMySQL |
| 存储 | MySQL 8.2（事实与审计）、Redis 8.8（热数据） |
| 测试 | pytest（单元 + 集成） |

### 5.3 存储职责划分

| 存储 | 承担 | 不承担 |
| --- | --- | --- |
| Redis | ① 特征滑动窗口（ZSET 按时间戳成员）② 事件幂等（`event_id → 首次决策结果`，TTL 24h）③ SSE 广播通道 ④ 实时计数器 | 不存最终事实，不做审计 |
| MySQL | 事件 / 特征快照 / 决策 / 命中明细 / 案件 / 处置 / 规则与版本 / 名单 / 模型版本 / 审计链 / 监控预聚合 | 不做毫秒级热计数 |

原则：**Redis 可丢，MySQL 不可丢**。Redis 重启后窗口数据从 `rc_event` 回溯重建（提供 `scripts/rebuild_windows.py`）。

---

## 6. 业务事件与模拟业务系统

### 6.1 五类事件规格

**通用信封字段**（所有事件共有）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `event_id` | string | 是 | 全局唯一，幂等键 |
| `event_type` | enum | 是 | `login` / `coupon_receive` / `order_create` / `order_pay` / `after_sale_apply` |
| `occurred_at` | string(ISO8601 UTC) | 是 | 业务发生时间 |
| `user_id` | string | 是 | 业务用户编号 |
| `phone` | string | 是 | 账号绑定手机号 |
| `device` | object | 是 | `device_id` + `fingerprint`{os, model, resolution, app_version, screen} |
| `network` | object | 是 | `ip` + `ip_region` + `is_proxy`(可选) |
| `address` | object | 否 | `receiver_name` / `phone` / `text` / `address_hash` |
| `payload` | object | 是 | 事件类型专有字段（见下） |

**各事件类型专有字段**：

| 事件类型 | 专有字段 |
| --- | --- |
| `login` | `login_type`(password/sms/scan)、`login_result`(success/fail) |
| `coupon_receive` | `coupon_id`、`coupon_name`、`face_value`、`campaign_id`、`channel` |
| `order_create` | `order_no`、`product_id`、`quantity`、`amount`、`pay_method` |
| `order_pay` | `order_no`、`pay_amount`、`pay_channel`、`pay_status` |
| `after_sale_apply` | `refund_no`、`order_no`、`refund_amount`、`reason`、`apply_type`(refund_only/return_refund) |

### 6.2 模拟业务系统

为承载"处置真实联动业务单据"的需求，系统内建最小业务域（5 张表）与模拟业务端：

| 表 | 说明 |
| --- | --- |
| `biz_customer` | 业务用户：账号、手机号、注册时间、状态（normal / blacklisted） |
| `biz_product` | 商品：名称、类目、单价 |
| `biz_order` | 订单：订单号、用户、商品、金额、状态（created / paid / cancelled / refunded） |
| `biz_refund` | 退款单：退款单号、订单号、金额、原因、状态（applied / approved / rejected） |
| `biz_coupon_receive` | 领券记录：券模板、面额、用户、渠道、领取时间 |

模拟业务端（`backend/app/simulator/`）能执行：注册、登录、领券、下单、支付、申请退款，并在每个动作前后调用风控网关，按决策结果决定是否继续（Reject 则拒绝该业务动作）。

### 6.3 事件接入契约

- 入口：`POST /api/v1/events`（单条）、`POST /api/v1/events/batch`（批量）。
- 鉴权：请求头 `X-API-Key`（对应 `sys_api_key` 表，可启停，记录最近调用时间）；前端用户接口走 JWT，两条通道分离。
- 幂等：`event_id` 在 `rc_event` 上有唯一约束；重复提交直接返回首次决策结果（Redis 命中则 <5ms 返回）。
- 校验失败：返回统一错误结构（错误码 + 字段路径 + 原因），不落库。
- 时间同步：`occurred_at` 与服务器时间偏差超过配置阈值（默认 5 分钟）时记录告警字段，不拒绝。

---

## 7. 特征工程

### 7.1 窗口档位

统一三档滑动窗口：**1 小时 / 24 小时 / 7 天**（落 `sys_config` 可调）。实现方式：Redis ZSET，score 为事件时间戳毫秒，member 为 `event_id`，按 `ZCOUNT key min max` 统计频次；辅助计数器 HASH 累计金额。

### 7.2 特征清单

| 组 | 特征键 | 含义 | 窗口 |
| --- | --- | --- | --- |
| 用户行为频次 | `user_login_cnt` | 同用户登录次数 | 1h/24h/7d |
| | `user_coupon_cnt` | 同用户领券次数 | 1h/24h/7d |
| | `user_coupon_amount` | 同用户领券面额合计 | 24h |
| | `user_order_cnt` | 同用户下单次数 | 1h/24h/7d |
| | `user_order_amount` | 同用户订单金额合计 | 24h |
| | `user_refund_cnt` | 同用户退款申请次数 | 24h/7d |
| | `user_refund_rate` | 退款申请数 / 订单数 | 24h |
| | `user_refund_amount` | 退款申请金额合计 | 24h |
| | `user_first_order_refund` | 首单即退款标记 | — |
| 设备环境 | `device_account_cnt` | 同设备关联账号数 | 24h/7d |
| | `device_coupon_cnt` | 同设备领券次数 | 1h |
| | `device_order_cnt` | 同设备下单次数 | 1h/24h |
| | `device_new_account_ratio` | 同设备新账号（注册 <7 天）占比 | 24h |
| | `device_env_risk` | 环境指纹风险分（模拟器/多开/异常分辨率等） | — |
| 网络 IP | `ip_account_cnt` | 同 IP 关联账号数 | 24h/7d |
| | `ip_coupon_cnt` | 同 IP 领券次数 | 1h |
| | `ip_order_cnt` | 同 IP 下单次数 | 1h |
| | `ip_region_mismatch` | IP 归属地与收货地不一致 | — |
| | `ip_is_datacenter` | IP 是否数据中心/代理段 | — |
| 收货地址 | `address_account_cnt` | 同收货地址关联账号数 | 7d |
| | `address_refund_cnt` | 同收货地址历史退款次数 | 7d |
| | `address_refund_rate` | 同收货地址退款率 | 7d |
| | `address_phone_share_cnt` | 同收货手机号关联账号数 | 7d |
| 主体画像 | `account_age_days` | 账号注册天数 | — |
| | `subject_case_cnt` | 近 30 天该主体风险案件数 | 30d |
| | `subject_blacklist` | 主体是否在名单库 | — |
| | `night_activity_ratio` | 夜间（0-6 点）行为占比 | 7d |

共 27 个特征键（含窗口展开后近百个取值为快照内容）。特征定义集中在 `backend/app/services/feature_engine.py` 的特征注册表，新增特征只需加一条声明。

### 7.3 特征快照

每次决策落一条 `rc_feature_snapshot`：`event_id`、特征键值 JSON、窗口档位、计算耗时、特征定义版本。仿真页与工作台按此渲染"实时特征快照对比"。

---

## 8. 决策引擎

流水线：**名单过滤 → 特征计算 → 规则求值 → 模型推理 → 融合仲裁 → 输出与落库**。

### 8.1 名单过滤

- 名单维度：`user` / `phone` / `ip` / `device` / `address`；类型：`black` / `white` / `gray`。
- **黑名单命中**：直接输出 `Reject`，跳过规则与模型打分（仍生成特征快照与命中记录，保证证据完整）。
- **白名单命中**：直接输出 `Pass`（跳过规则与模型），仍记录留痕。
- **同时命中**：按 `rc_list_entry.priority` 决定，默认黑名单优先（`sys_config: list_conflict_policy = black_first`）。
- 灰名单：不直接决定动作，转为一组加成特征（如 `subject_gray_flag=1`）参与规则与模型。

### 8.2 规则引擎

**条件表达能力：双模（树 ⇄ 表达式）**，后端统一解析为同一棵 AST。

AST 节点：`and` / `or` / `not` / `condition`；`condition = {field, op, value}`，`op ∈ {>, >=, <, <=, ==, !=, in, not_in, between, contains, matches}`。

表达式文本示例：

```
device_account_cnt_24h >= 3 and user_coupon_cnt_1h > 5 and account_age_days < 7
```

求值语义：

- 字段缺失 → 该条件判定为 `false`，同时写入 `missing_fields` 告警（默认宽容模式；`sys_config: rule_strict_mode=true` 时改为求值失败并记错误）。
- 一条规则在一次决策中最多计分一次（避免重复累加）。
- 求值器为自研、白名单式（无 `eval`），只允许特征字段名、字面量与上述运算符。

**规则字段**：`code`、`name`、`scene`、`category`、`condition`(AST JSON)、`condition_text`、`score`、`action_hint`(`none`/`challenge`)、`priority`、`enabled`、`version`、`description`。

**规则分类**（共 5 类，与预置种子对应）：`list` 名单类、`frequency` 频次聚集类、`environment` 环境指纹类、`aftersale` 售后欺诈类、`amount` 金额类。

**版本留痕**：每次修改规则写入 `rc_rule_version` 快照（含变更人、时间、前后 AST），支持查看历史版本与回滚。

### 8.3 模型引擎

**离线训练**（`scripts/train_model.py`）：

1. 从 `rc_event` + `rc_feature_snapshot` + 场景标签构造样本集（正样本 = 两个作弊场景脚本产生的账号事件；负样本 = 正常事件），按时间 7:3 切分训练/验证集。
2. 特征向量取 20 维子集（见下），数值经 `log1p` 压缩长尾后做 z-score 标准化（均值/方差来自训练集）。
3. 模型：**逻辑回归**（scikit-learn），输出可解释权重向量；同时训练一个孤立森林作为对照指标（不参与线上决策）。
4. 产出 `backend/models_artifacts/model.json`：

```json
{
  "version": "v1",
  "trained_at": "2026-09-23T10:00:00Z",
  "feature_names": ["device_account_cnt_24h", "user_coupon_cnt_1h"],
  "mean": [0.0, 0.0],
  "std": [1.0, 1.0],
  "weights": [0.82, 1.15],
  "intercept": -2.31,
  "metrics": { "auc": 0.94, "ks": 0.71, "precision_at_100": 0.88, "samples": 50000 }
}
```

**20 维模型输入**：`device_account_cnt_24h`、`device_new_account_ratio_24h`、`device_coupon_cnt_1h`、`device_env_risk`、`ip_account_cnt_24h`、`ip_coupon_cnt_1h`、`ip_is_datacenter`、`ip_region_mismatch`、`user_coupon_cnt_1h`、`user_coupon_amount_24h`、`user_order_cnt_1h`、`user_refund_cnt_24h`、`user_refund_rate_24h`、`user_refund_amount_24h`、`user_first_order_refund`、`address_account_cnt_7d`、`address_refund_rate_7d`、`address_phone_share_cnt_7d`、`account_age_days`、`night_activity_ratio_7d`。

**在线推理**（`backend/app/services/model_engine.py`）：启动时加载启用的 `model.json`；取特征 → 缺失值用训练均值填充 → 标准化 → `z = w·x + b` → `p = sigmoid(z)` → **模型分 = round(100 × p)**。

**可解释性**：对每个特征计算贡献 `w_i × z_i`，按绝对值排序输出 **top-5 贡献特征**（正贡献推高、负贡献拉低），落 `rc_model_contribution`，在仿真页与工作台展示"模型关注点"。

**版本治理**：`rc_model_version` 记录版本、指标、启用状态；同一时刻仅一个启用版本；页面可查看历史版本指标并切换（切换动作入审计）。

### 8.4 融合与仲裁

```
综合分 risk_score = clamp(round(rule_score + α × model_score), 0, 100)，α 默认 0.3（可配）
```

| 综合分 | 风险等级 | 决策动作 | 附加行为 |
| --- | --- | --- | --- |
| 0 ~ 59 | 低（low） | `Pass` | 正常放行 |
| 60 ~ 79 | 中（mid） | `Review` | 生成人工审核案件；若命中规则带 `action_hint=challenge` 则改为 `Challenge`（触发二次验证，不建案） |
| 80 ~ 100 | 高（high） | `Reject` | 直接拦截 + 自动创建风险案件 |

补充规则：

- 三分并存：`rule_score`、`model_score`、`risk_score` 全部落库并在界面并排展示，用于解释"为什么是这个结论"。
- 融合模式可配（`sys_config: fusion_mode`）：`additive`（默认）/ `max` / `weighted`。
- **Challenge 仅由规则显式声明触发**，不做隐式区间映射，保证策略师可控。
- 名单直接命中时，规则分与模型分置 0 并在决策记录中标注 `decided_by = list`。

### 8.5 决策输出结构（对应 `项目实战.md` §2.3.8）

| 区块 | 内容 |
| --- | --- |
| 事件基本信息 | 事件编号、事件类型、涉事用户编号、业务单据编号、请求时间戳 |
| 决策结果 | 综合分（0~100）、规则分、模型分、风险等级（低/中/高）、处理建议（Pass/Challenge/Review/Reject）、案件编号（如有）、模型版本 |
| 规则命中明细 | 规则编码、规则名称、单项风险分值、触发原因描述 |
| 特征上下文快照 | 行为频次、环境指纹、设备聚集度等键值结构 |
| 模型判据 | 模型分、top-5 贡献特征与贡献值 |
| 审核留痕信息 | 案件编号、处理人员、处理结论、处置动作明细、审核时间（处置后回填） |
| 运行信息 | 决策耗时（ms）、是否命中幂等缓存、事件来源（real/simulation） |

---

## 9. 案件流转与处置

### 9.1 案件生成与合案

- 触发条件：决策动作为 `Review` 或 `Reject` 时自动生成案件。
- 合案键：`(主体类型, 主体值, 场景)`；合案窗口默认 **30 分钟**（`sys_config: case_merge_window_minutes`）。
- 窗口内重复触发 → 不新建案件，累加到既有案件：`hit_cnt + 1`、`event_cnt + 1`、刷新 `max_score` 与 `last_at`，并把新事件挂到 `rc_case_event`。
- 案件字段：案件编号、主体、场景、状态、风险等级、最高分、命中次数、关联事件数、首次/末次触发时间、当前处理人、创建时间。

### 9.2 状态机

```
待审 pending ──接手/锁定 claim──> 审核中 processing ──提交处置 dispose──> 已处置 disposed ──归档 archive──> 已归档 archived
      └──────────────────────── 管理员强制关闭 close（异常兜底，可选） ────────────────────────┘
```

| 迁移 | 触发者 | 前置条件 | 副作用 |
| --- | --- | --- | --- |
| `pending → processing` | auditor / admin 点击"接手" | 当前无处理人（乐观锁：条件更新 `WHERE status='pending'`） | 记录 `handler` 与接手时间，入审计 |
| `processing → disposed` | 当前 `handler` | 已填写业务结论与至少一个风控动作 | 执行处置联动（§9.4），入审计 |
| `disposed → archived` | admin 手动归档（支持批量） | 无 | 入审计 |
| `pending/processing → closed` | admin | 需填原因 | 入审计 |

### 9.3 处置结论（双维度）

**业务结论（单选）**：

| 值 | 含义 | 作用对象 |
| --- | --- | --- |
| `approve`（通过） | 业务正常继续 | 订单放行 / 退款通过 |
| `reject`（驳回） | 业务终止 | 取消订单 / 驳回退款申请 |

**风控动作（多选）**：

| 值 | 含义 |
| --- | --- |
| `pass` | 放行（不追加措施） |
| `block_order` | 拦截关联订单（未支付订单置为已取消） |
| `blacklist_user` | 拉黑账号（写入黑名单库 + 业务用户置黑） |
| `ban_device` | 封禁设备（设备加入黑名单） |
| `watchlist_add` | 加入灰名单（继续观察） |

处置时必须填写**处置原因备注**（≥10 字），提交前有二次确认弹窗。

### 9.4 处置联动

| 动作 | 联动效果 |
| --- | --- |
| `block_order` | `biz_order.status = cancelled`（仅 `created` 未支付订单可取消；已支付订单记录"需人工退款"提示） |
| `reject` + 售后场景 | `biz_refund.status = rejected` |
| `blacklist_user` | 新增 `rc_list_entry`（black/user），`biz_customer.status = blacklisted` |
| `ban_device` | 新增 `rc_list_entry`（black/device） |
| `watchlist_add` | 新增 `rc_list_entry`（gray/user） |

所有联动结果写入 `rc_case_action_item` 与审计日志；联动失败不阻塞处置提交，但记录失败原因并在界面提示。

---

## 10. 审计与模型治理

### 10.1 审计范围

登录、案件接手、处置提交、案件归档，规则增删改 / 启停 / 回滚，名单增删改 / 批量导入，模型版本切换，系统配置变更。

### 10.2 哈希链

- 表 `rc_audit_log` 仅允许追加：`INSERT` / `SELECT`；应用数据库账号不授予 `UPDATE` / `DELETE`，并加触发器二次兜底。
- 每条记录含 `prev_hash`（上一条的 `hash`）与 `hash = sha256(prev_hash + 规范化业务字段串)`。
- 提供 `GET /api/v1/audit/verify`：全链重算并返回 `{total, valid, first_broken_id}`；"审计与模型"页提供一键校验按钮——可用于现场演示"手工改一条记录 → 校验立即报链断裂"。

### 10.3 模型治理

"审计与模型"页展示模型版本列表（版本号、AUC/KS、训练样本数、训练时间、启用状态），支持切换启用版本；每次切换入审计。重训流程：`scripts/gen_dataset.py` → `scripts/train_model.py` → 新版本入 `rc_model_version`。

---

## 11. 页面需求

### 11.1 登录页（P1）

- 目标：三种角色分别登录进入各自工作区。
- 内容：账号密码表单、错误提示、登录按钮。
- 交互：登录成功按角色跳转默认页（auditor → 审核工作台；strategist → 策略配置；admin → 态势大盘）；JWT 存 `localStorage`，过期跳回登录页。

### 11.2 风控态势大盘（P2）

- 目标：一屏掌握实时风控态势。
- 区块：
  1. **指标卡**：实时事件请求量（近 1 分钟）、拦截率、人审率、预估挽回资损（标注"估算口径"）。
  2. **拦截率趋势折线图**：按分钟/小时聚合，支持"今日 / 近 7 天"切换。
  3. **风险等级分布环形图**：低/中/高占比。
  4. **规则命中排行榜**：Top 10 规则（命中次数 + 累计分值）。
  5. **实时事件滚屏**：SSE 推送最新决策，含事件类型、主体、综合分、决策动作、命中规则摘要。
- 交互：时间范围切换、事件滚屏暂停/继续、点击滚屏条目跳转决策详情。
- 状态：加载骨架、空态（提示"当前无事件，可在仿真页注入或运行事件生成器"）、SSE 断线自动重连并提示。
- 口径：默认**排除** `source=simulation` 的事件，页面提供"包含仿真事件"开关。

### 11.3 风控审核工作台（P1）

- 目标：审核员在一屏内完成"看证据 → 判风险 → 做处置"。
- 布局：**三栏一体**（不拆独立路由）。
  - **左栏 案件筛选与列表区**：按风险等级、事件场景、审核状态、触发时间筛选；支持案件号/主体搜索；分页列表展示案件号、主体、场景、风险等级、最高分、命中次数、状态、处理人、触发时间与风险标签。
  - **中栏 证据链与画像详情区**：
    - 用户画像卡（可折叠）：账号年龄、注册渠道、历史案件数、当前名单状态、近 7 天行为概览。
    - 当前业务单据：订单/退款单信息（金额、商品、地址、状态）。
    - 实时特征快照：按分组表格展示，并对**窗口对比**做高亮（如 1h vs 24h 频次突增）。
    - 关联实体图谱：ECharts 力导向图，中心为当前主体，连接同设备 / 同 IP / 同地址 / 同手机号的关联账号，节点大小表示关联强度，颜色表示风险等级。
    - 命中规则与模型判据：规则命中明细表 + 模型 top-5 贡献条形图。
  - **右栏 研判与处置协同区**：系统判定摘要（综合分/等级/建议）、证据提示、业务结论单选、风控动作多选（含副作用说明）、处置原因备注、关联名单添加提示、二次确认弹窗、一键执行按钮。
- 状态：列表空态、详情加载态、处置提交成功/失败反馈、并发接手冲突提示（"案件已被 XXX 接手"）。

### 11.4 策略与规则配置页（P2）

- 目标：策略师自助配置规则与名单。
- 规则列表：按场景/分类筛选、关键词搜索；列展示规则编码、名称、场景、分类、单条分值、`action_hint`、启停开关、优先级、版本、更新时间。
- 规则编辑：条件树构建器（与/或/非 + 条件行，字段下拉来自特征注册表）⇄ 表达式文本**双向切换编辑**；分值输入、`action_hint` 选择、启停、优先级、描述；保存前实时"试算"（用样例特征校验表达式是否合法）。
- 版本：查看历史版本、对比与回滚。
- 名单库标签页：黑/白/灰 × 用户/手机号/IP/设备/地址 的分类浏览、增删改、有效期设置、批量导入（CSV/Excel，展示导入批次结果与失败行原因）。
- 权限：仅 strategist / admin 可写。

### 11.5 事件仿真测试页（P2）

- 目标：单条事件模拟检测与判定链路回溯。
- 内容：
  1. 用例模板加载（预置"羊毛党领券""恶意退款"等模板一键填充）。
  2. 结构化事件参数表单（按事件类型动态渲染字段）+ JSON 原始视图双向同步。
  3. 执行检测：调用决策接口，**单步呈现**：① 特征提取结果（分组表格）② 名单匹配结果 ③ 规则命中链路（逐条展示求值结果，含未命中规则）④ 模型贡献 ⑤ 最终决策输出。
  4. 历史仿真记录列表，可回看链路（仿真事件 `source=simulation`，默认不计入大盘）。

### 11.6 审计与模型页（P2，管理员专属）

- **审计流水**：按操作人、动作类型、对象类型、时间范围筛选；列表展示操作摘要，展开可见前后值 diff。
- **链完整性校验**：一键校验按钮，展示总条数、有效条数、首个断裂记录（可直接跳转）。
- **模型版本**：版本列表（版本号、AUC/KS、样本数、训练时间、启用状态）、切换启用、查看特征权重表。

---

## 12. 接口清单（概览）

| 方法与路径 | 说明 | 权限 |
| --- | --- | --- |
| `POST /api/v1/auth/login` | 登录换取 JWT | 公开 |
| `GET /api/v1/auth/me` | 当前用户与角色 | 登录 |
| `POST /api/v1/events`、`/events/batch` | 事件接入（决策） | API Key |
| `GET /api/v1/decisions`、`/{id}` | 决策查询与详情（含命中、特征、模型判据） | 登录 |
| `GET /api/v1/cases`、`/{case_no}` | 案件列表与详情（含画像、特征、图谱数据） | 登录 |
| `POST /api/v1/cases/{case_no}/claim` | 接手案件 | auditor/admin |
| `POST /api/v1/cases/{case_no}/dispose` | 提交处置 | auditor/admin |
| `POST /api/v1/cases/{case_no}/archive` | 归档（支持批量） | admin |
| `GET/POST/PUT /api/v1/rules`、`/{code}/toggle`、`/{code}/versions`、`/{code}/rollback` | 规则管理 | strategist/admin |
| `POST /api/v1/rules/validate` | 表达式/条件树试算校验 | strategist/admin |
| `GET/POST/DELETE /api/v1/lists`、`/lists/import`、`/lists/imports/{id}` | 名单管理 | strategist/admin |
| `GET /api/v1/dashboard/summary`、`/trend`、`/distribution`、`/rule-ranking`、`/loss` | 大盘指标 | 登录 |
| `GET /api/v1/stream/events` | SSE 实时决策流 | 登录 |
| `POST /api/v1/simulation/run`、`/simulation/templates` | 仿真执行与模板 | strategist/admin |
| `GET /api/v1/audit`、`/audit/verify` | 审计查询与链校验 | admin |
| `GET /api/v1/models`、`POST /api/v1/models/{version}/activate` | 模型版本管理 | strategist/admin |
| `GET/PUT /api/v1/configs` | 系统配置（阈值/α/窗口/融合模式/冲突策略） | strategist/admin |

---

## 13. 数据模型（表清单）

| 域 | 表 | 阶段 |
| --- | --- | --- |
| 系统 | `sys_user`（预置三角色账号） | P0 |
| 系统 | `sys_config`（阈值、α、合案窗口、窗口档位、融合模式、冲突策略、严格模式） | P0 |
| 系统 | `sys_api_key`（业务端密钥） | P0 |
| 业务 | `biz_customer`、`biz_product`、`biz_order`、`biz_refund`、`biz_coupon_receive` | P0 |
| 事件 | `rc_event` | P0 |
| 特征 | `rc_feature_snapshot` | P0 |
| 决策 | `rc_decision`、`rc_decision_hit`、`rc_model_contribution` | P0 |
| 规则 | `rc_rule`、`rc_rule_version` | P0 |
| 名单 | `rc_list_entry` | P0 |
| 名单 | `rc_list_import` | P2 |
| 案件 | `rc_case`、`rc_case_event`、`rc_case_action`、`rc_case_action_item` | P1 |
| 模型 | `rc_model_version` | P0（表）/ P2（页面） |
| 审计 | `rc_audit_log` | P0 |
| 监控 | `rc_metric_daily` | P2 |

索引要点：`rc_event(event_id)` 唯一、`rc_event(user_id, occurred_at)`、`rc_event(device_id, occurred_at)`、`rc_event(ip, occurred_at)`；`rc_decision(event_id)` 唯一；`rc_case(subject_value, scene, status)`；`rc_list_entry(list_type, dimension, value)` 唯一；`rc_audit_log` 主键自增即链序。

---

## 14. 数据集与作弊场景

### 14.1 数据规模

| 项 | 取值 |
| --- | --- |
| 事件总量 | 50,000 条 |
| 时间跨度 | 14 天 |
| 账号总数 | 约 800 个（含 60~80 个作弊账号） |
| 作弊样本占比 | 6% ~ 8% |
| 实时流速率 | 3~5 事件/秒（可调） |
| 随机种子 | 固定（默认 `20260923`），结果可复现 |
| 事件类型分布 | `login` 35%、`order_create` 20%、`coupon_receive` 18%、`order_pay` 15%、`after_sale_apply` 12% |

### 14.2 场景一：大促羊毛党批量领券（主演示）

- 剧本：攻击者用设备农场注册 40 个新账号，共用 5 个设备指纹与 3 个代理 IP，集中领取高面额券（单券 50~200 元）。
- 命中路径：`device_account_cnt_24h >= 3` + `device_coupon_cnt_1h > 5` + `account_age_days < 7` + `ip_is_datacenter` → 规则分累加至 80+ → `Reject` + 自动建案。
- 期望结果：拦截至高面额券，案件证据链呈现同设备账号聚集图谱，审核员确认后执行 `blacklist_user` + `ban_device`。

### 14.3 场景二：高价值商品恶意退款欺诈

- 剧本：某用户在 24 小时内对 3 笔高价值订单（单笔 2,000~8,000 元）发起"未收到货"仅退款；其收货地址在 7 天内关联过 4 个账号的历史退款记录。
- 命中路径：`user_refund_cnt_24h >= 3` + `user_refund_amount_24h` 超阈值 + `address_refund_rate_7d` 偏高 → 规则分落在 60~79 → `Review` 建案。
- 期望结果：审核员查看地址关联退款历史与特征快照后，选择 `reject`（驳回退款）+ `blacklist_user`，`biz_refund.status` 置为 `rejected`。

---

## 15. 指标口径（大盘）

| 指标 | 口径 |
| --- | --- |
| 实时事件请求量 | 近 1 分钟决策条数（Redis 计数） |
| 拦截率 | `Reject 条数 / 全部决策条数`（含名单拦截） |
| 人审率 | `Review 条数 / 全部决策条数` |
| 风险等级分布 | 今日低/中/高占比 |
| 规则命中排行 | 今日命中次数 Top 10（含累计分值） |
| 预估挽回资损 | 被 `Reject` 拦截的金额 + `Review` 后人工驳回的金额（订单取订单金额，退款取退款申请金额），页面标注"估算口径" |
| 统计范围 | 默认排除 `source=simulation`，页面可开关 |

---

## 16. 非功能需求

| 类别 | 要求 |
| --- | --- |
| 性能 | 单次决策 P95 < 50ms（本地，落库异步化）；特征计算 < 20ms；SSE 推送延迟 < 1s |
| 数据一致 | 决策响应与落库解耦：响应同步返回，快照与命中明细异步落库（失败重试 + 补偿日志） |
| 安全 | JWT + 角色权限校验；事件入口 API Key 独立通道与独立审计标识；密码 bcrypt 存储；审计表只增不改 |
| 可观测 | 结构化日志（含 `decision_id` 贯穿）；健康检查 `/healthz`；决策耗时与命中率埋点 |
| 可维护 | 中文注释（意图、约束、坑点）；特征与规则集中注册；配置项一律入 `sys_config`，不硬编码 |
| 编码 | 后端类型标注 + Pydantic 校验；前端 TS 严格模式；统一错误响应结构 |

---

## 17. 分期交付与验收

### 17.1 各期交付物

| 阶段 | 交付物 | 可验证形式 |
| --- | --- | --- |
| **P0** | 库表与迁移、种子数据（3 账号 / 20 规则 / 名单 / API Key）、模拟业务端、事件网关、特征引擎、名单+规则+模型+融合、决策落库与幂等、审计链、单元与集成测试 | `scripts/init_db.ps1` + `scripts/verify_p0.ps1` 全绿；pytest 通过 |
| **P1** | 案件流转（合案 + 状态机）、处置联动、审核工作台页面 | 工作台完成一次"接手 → 查看证据 → 处置 → 订单/退款联动生效"闭环 |
| **P2** | 大盘（SSE）、策略与规则配置页、事件仿真页、审计与模型页、监控预聚合 | 六个页面可交互；审计链校验可演示断裂检测 |
| **P3** | 全量数据集（5 万事件）、两个作弊场景脚本、脚本化验收清单、README 与演示手册 | 按验收清单逐条复跑通过 |

### 17.2 验收标准映射（`项目实战.md` §2.3.10）

| # | 验收标准 | 承载阶段 | 验证方式 |
| --- | --- | --- | --- |
| 1 | 接入并正确解析五类事件 | P0 | `verify_p0.ps1`：五类事件各注入一条，校验解析与落库 |
| 2 | 实时计算行为频次与环境特征并生成快照 | P0 | 单测（窗口计数）+ 快照接口校验 |
| 3 | 按规则集完成评分与仲裁，正确输出 Pass/Review/Reject | P0 | 单测（评分/仲裁/名单优先级）+ 集成测试三条路径 |
| 4 | 中高风险自动创建案件并在工作台展示 | P1 | 触发场景 → 案件列表出现并可打开 |
| 5 | 工作台查看画像、特征快照、关联证据，完成复核与处置闭环 | P1 | 人工走查 + 处置后业务单据状态校验 |
| 6 | 新增、编辑、启停规则，维护黑白名单 | P2 | 页面操作 + 接口断言 |
| 7 | 仿真页完成单条事件模拟检测与链路回溯 | P2 | 载入模板 → 执行 → 五段链路可见 |
| 8 | 大盘查看实时拦截率、风险分布与规则命中排行 | P2 | 页面走查 + SSE 滚屏观察 |
| 9 | 完成至少一个典型作弊对抗场景全流程演示 | P3 | 场景脚本一键重放，从注入到处置全链路复现 |

---

## 18. 风险与开放问题

| # | 风险 / 待确认 | 影响 | 应对 |
| --- | --- | --- | --- |
| 1 | 本地托管的 MySQL / Redis 需保持运行 | 后端不可用 | `dev.ps1` 启动前做依赖健康检查并给出明确提示 |
| 2 | 模型指标基于合成数据，AUC 偏乐观 | 答辩质疑"模型太好看" | PRD 与页面均标注"合成数据集训练，指标仅供演示" |
| 3 | UI 规范 `DESIGN.md` 由我生成，你后续会调整 | 页面返工 | 视觉规范与代码解耦（AntD token + 主题文件），改规范只改 token |
| 4 | 决策响应与落库解耦可能造成短暂不一致 | 大盘短时抖动 | 落库失败重试 + 补偿日志；大盘以落库数据为准并标注延迟 |
| 5 | 多人并发接手同一案件 | 处置冲突 | 乐观锁（状态 + 处理人条件更新），冲突时前端提示并刷新 |
| 6 | 审计表禁用 UPDATE/DELETE 依赖数据库账号权限 | 权限若被放宽则形同虚设 | 权限 + 触发器双保险，并提供链校验作为最终兜底 |

---

## 附录 A：P0 库表清单（含关键字段）

| 表 | 关键字段 |
| --- | --- |
| `sys_user` | id, username, password_hash, real_name, role, status, created_at |
| `sys_config` | id, config_key, config_value, value_type, description, updated_by, updated_at |
| `sys_api_key` | id, name, api_key_hash, enabled, last_used_at, created_at |
| `biz_customer` | id, user_id, phone, register_at, register_channel, level, status |
| `biz_product` | id, product_id, name, category, price |
| `biz_order` | id, order_no, user_id, product_id, quantity, amount, status, address_hash, created_at |
| `biz_refund` | id, refund_no, order_no, user_id, refund_amount, reason, apply_type, status, created_at |
| `biz_coupon_receive` | id, receive_no, user_id, coupon_id, face_value, channel, created_at |
| `rc_event` | id, event_id(uk), event_type, user_id, phone, device_id, device_fingerprint(json), ip, ip_region, address_hash, biz_no, occurred_at, received_at, source, payload(json), latency_ms |
| `rc_feature_snapshot` | id, event_id, features(json), window_profile, calc_cost_ms, feature_version, created_at |
| `rc_decision` | id, decision_id, event_id(uk), rule_score, model_score, risk_score, risk_level, action, action_hint, decided_by, fusion_alpha, model_version, case_no, hit_count, latency_ms, from_cache, created_at |
| `rc_decision_hit` | id, decision_id, rule_code, rule_name, rule_category, score, reason, evidence(json) |
| `rc_model_contribution` | id, decision_id, feature_name, feature_value, contribution, direction, rank |
| `rc_rule` | id, code(uk), name, scene, category, condition(json), condition_text, score, action_hint, priority, enabled, version, description, created_by, updated_by, created_at, updated_at |
| `rc_rule_version` | id, rule_code, version, condition(json), condition_text, score, action_hint, enabled, change_type, changed_by, changed_at |
| `rc_list_entry` | id, list_type, dimension, value, priority, reason, source, expire_at, status, created_by, created_at, updated_at（`list_type+dimension+value` 唯一） |
| `rc_model_version` | id, version(uk), file_path, feature_names(json), metrics(json), sample_count, trained_at, active, remark, created_at |
| `rc_audit_log` | id, actor_id, actor_name, role, action, target_type, target_id, before_json, after_json, reason, prev_hash, hash, created_at |

## 附录 B：仓库文件清单（P0 目标态）

```
commerce-risk-control-system/
├─ AGENTS.md                      # 协作规范（自本地引入）
├─ README.md                      # 环境准备 / 启动 / 验收 / 演示
├─ 项目实战.md                    # 原始需求
├─ .gitignore  .env.example
├─ docs/
│  ├─ PRD.md                      # 本文档
│  ├─ DESIGN.md                   # UI 设计规范（后续可调整）
│  └─ P0-验收清单.md              # 阶段验收（P0 完成后生成）
├─ scripts/
│  ├─ dev.ps1                     # 一键启动后端 + 前端
│  ├─ init_db.ps1                 # 建库 + 迁移 + 种子
│  ├─ gen_dataset.py              # 数据集与场景生成
│  ├─ train_model.py              # 离线训练 → model.json
│  ├─ rebuild_windows.py          # Redis 窗口重建
│  └─ verify_p0.ps1               # P0 验收脚本
├─ backend/
│  ├─ requirements.txt  alembic.ini  alembic/versions/
│  ├─ app/
│  │  ├─ main.py
│  │  ├─ core/        # settings / security(JWT+APIKey) / logging / deps
│  │  ├─ db/          # session / base
│  │  ├─ models/      # 按域拆分
│  │  ├─ schemas/     # 事件 / 决策 / 案件 / 规则 / 名单 / 审计
│  │  ├─ api/         # gateway, decisions, cases, rules, lists,
│  │  │               # dashboard, simulation, audit, auth, models, configs
│  │  ├─ services/    # event_gateway, feature_engine, rule_engine,
│  │  │               # model_engine, fusion, list_service, case_service,
│  │  │               # disposal_service, audit_service, metrics_service
│  │  ├─ expression/  # lexer / parser / ast / evaluator
│  │  ├─ simulator/   # 模拟业务端 + 事件流回放
│  │  └─ seeds/       # 账号 / 规则 / 名单 / API Key / 场景脚本
│  ├─ models_artifacts/model.json
│  └─ tests/          # 窗口 / 表达式 / 仲裁 / 名单优先级 / 集成
└─ frontend/
   ├─ package.json  vite.config.ts  tsconfig.json  index.html
   └─ src/
      ├─ main.tsx  App.tsx  router.tsx
      ├─ api/       # 请求封装 + TanStack Query hooks
      ├─ store/     # 认证与全局状态
      ├─ layouts/   # 主框架（侧边栏 / 顶栏 / 角色菜单）
      ├─ pages/     # Login / Dashboard / ReviewWorkbench /
      │             # PolicyConfig / Simulation / AuditModel
      ├─ components/# 证据链 / 画像卡 / 实体图谱 / 条件树编辑器 / 状态标签
      ├─ theme/     # AntD token（对应 DESIGN.md）
      └─ types/     # 与后端 schema 对齐的 TS 类型
```
