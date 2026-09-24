/**
 * 与后端 schema 对齐的类型（app/schemas/*.py 的镜像）。
 *
 * 手工维护而非 codegen：P0 的 schema 面很小（认证 + 事件），
 * 引入 codegen 的成本高于收益；等接口到 P2 规模再评估。
 */

/** 后端统一响应封装（app/api/response.py）：code 是**字符串**，成功恒为 "0"。 */
export interface ApiOk<T> {
  code: string;
  message: string;
  data: T;
  trace_id: string;
}

/** 失败响应体：data 恒为 null，field/detail 用于表单级提示。 */
export interface ApiFailure {
  code: string;
  message: string;
  data: null;
  trace_id: string;
  field?: string | null;
  detail?: string | null;
}

/** app/schemas/auth.py::UserOut */
export interface CurrentUser {
  id: number;
  username: string;
  real_name: string | null;
  role: Role;
  status: string;
}

export type Role = "admin" | "strategist" | "auditor";

export type DecisionAction = "Pass" | "Challenge" | "Review" | "Reject";
export type RiskLevel = "low" | "mid" | "high";

/** app/main.py::healthz（依赖不健康时 HTTP 503，结构相同） */
export interface HealthZ {
  app: string;
  env: string;
  status: "ok" | "degraded";
  mysql: { status: string; version?: string; database?: string; detail?: string };
  redis: { status: string; version?: string; detail?: string };
}

/** 展示名：真实姓名优先，缺失时退回用户名（在 UI 层统一处理，避免各处 ?? 判断）。 */
export function displayName(user: CurrentUser | null): string {
  if (!user) return "未登录";
  return user.real_name || user.username;
}

/* -------------------------------------------------------------------------- */
/* 案件域（app/services/case_service.py 的字典输出镜像）                        */
/*                                                                            */
/* 这里有两点与后端约定的**口径**必须保持一致，否则界面会显示"看起来对但错了"：*/
/* 1. `handler` 已做转换（后端存 handler 名称 + handler_id 两列），前端只读展示；*/
/* 2. 所有时间为 naive UTC 的 ISO8601（无时区后缀），展示前必须 +8 小时，       */
/*    统一走 `utils/datetime.ts`，禁止在各处 `new Date()` 直接格式化。          */
/* -------------------------------------------------------------------------- */

/** 案件状态机（PRD §9.2）：pending → processing → disposed → archived，另有 closed 兜底。 */
export type CaseStatus = "pending" | "processing" | "disposed" | "archived" | "closed";

/** 事件场景：登录 / 领券 / 下单 / 售后。 */
export type CaseScene = "login" | "coupon" | "order" | "after_sale";

/** 业务结论（单选，PRD §9.3）。 */
export type BizResult = "approve" | "reject";

/** 风控动作（多选，PRD §9.3）；`pass` 与其他动作互斥。 */
export type RiskAction = "pass" | "block_order" | "blacklist_user" | "ban_device" | "watchlist_add";

/** 联动执行结果三档（PRD §9.4）：success 已执行 / skipped 预期内无对象 / failed 需排查。 */
export type ExecResult = "success" | "skipped" | "failed";

/** 案件表行（列表与详情共用同一形状）。 */
export interface CaseRow {
  case_no: string;
  subject_type: string;
  subject_value: string;
  scene: CaseScene;
  status: CaseStatus;
  risk_level: RiskLevel;
  max_score: number;
  /** 累计命中规则条数（与 event_cnt 口径不同，PRD §9.1）。 */
  hit_cnt: number;
  /** 关联事件数。 */
  event_cnt: number;
  first_at: string | null;
  last_at: string | null;
  handler: string | null;
  handler_id: number | null;
  claimed_at: string | null;
  dispose_result: BizResult | null;
  disposed_at: string | null;
  archived_at: string | null;
  closed_at: string | null;
  close_reason: string | null;
  created_at: string | null;
}

/** GET /cases 的 data：分页 + 各状态计数（筛选标签上的数字）。 */
export interface CaseListResult {
  items: CaseRow[];
  total: number;
  page: number;
  size: number;
  status_counts: Record<string, number>;
}

/** 案件列表筛选条件（与后端 query 参数一一对应）。 */
export interface CaseFilters {
  status?: CaseStatus;
  risk_level?: RiskLevel;
  scene?: CaseScene;
  keyword?: string;
  mine?: boolean;
  page: number;
  size: number;
}

/** 案件关联事件（时间线）。 */
export interface CaseTimelineItem {
  event_id: string;
  decision_id: string | null;
  event_type: string;
  scene: CaseScene;
  action: DecisionAction;
  risk_level: RiskLevel;
  risk_score: number;
  hit_count: number;
  biz_no: string | null;
  occurred_at: string | null;
}

/** 时间线对应的决策摘要（提供规则分 / 模型分对比）。 */
export interface CaseDecisionSummary {
  event_id: string;
  decision_id: string | null;
  action: DecisionAction;
  risk_level: RiskLevel;
  risk_score: number;
  rule_score: number | null;
  model_score: number | null;
  model_version: string | null;
}

/** 画像卡里的名单条目（只含该主体自己的名单记录，不含设备/地址关联）。 */
export interface ProfileListEntry {
  list_type: string;
  dimension: string;
  reason: string | null;
  expire_at: string | null;
}

/** 用户画像卡（PRD §11.3 中栏第一块）。 */
export interface CaseProfile {
  subject_value: string;
  customer: {
    user_id: string;
    phone: string | null;
    level: string | null;
    status: string | null;
    register_channel: string | null;
    register_at: string | null;
  } | null;
  account_age_days: number | null;
  list_status: ProfileListEntry[];
  behavior: { event_cnt_7d?: number; case_cnt_30d?: number };
}

/** 订单单据卡。`missing=true` 表示事件引用的订单在业务表查不到（数据不一致）。 */
export interface OrderDoc {
  kind: "order";
  order_no: string;
  user_id?: string;
  product_id?: string;
  quantity?: number;
  amount?: number;
  status?: string;
  missing: boolean;
}

/** 退款单据卡（售后场景；带关联订单，便于判断"退的是哪一单的多少钱"）。 */
export interface RefundDoc {
  kind: "refund";
  refund_no: string;
  order_no?: string;
  user_id?: string;
  refund_amount?: number;
  reason?: string;
  status?: string;
  missing: boolean;
  order?: {
    order_no: string;
    product_id: string;
    quantity: number;
    amount: number;
    status: string;
  } | null;
}

/** 登录 / 领券场景没有业务单据，此时 biz_doc 为 null。 */
export type BizDoc = OrderDoc | RefundDoc;

/** 图谱节点：中心是案件主体，其余是同设备 / 同 IP / 同地址 / 同手机号的账号。 */
export interface GraphNode {
  id: string;
  label: string;
  risk_level: RiskLevel;
  case_cnt: number;
  is_center: boolean;
  size: number;
}

/** 图谱边：`label` 是关系类型（device / ip / address / phone）。 */
export interface GraphEdge {
  source: string;
  target: string;
  label: string;
}

export interface EntityGraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/** 特征快照分组（后端按 agg 类型分组：频次 / 金额 / 聚集度 / 比例 / 名单与标记 / 画像）。 */
export interface FeatureItem {
  key: string;
  value: unknown;
  description: string;
}

export interface FeatureGroup {
  group: string;
  items: FeatureItem[];
}

/** 规则命中明细里的一条条件证据（与后端表达式求值器同构）。 */
export interface RuleEvidence {
  op: string;
  field: string;
  actual: unknown;
  expected: unknown;
  passed: boolean;
  missing: boolean;
  detail: string | null;
}

/** 命中的规则（按分值降序）。 */
export interface HitRule {
  rule_code: string;
  rule_name: string;
  rule_category: string;
  score: number;
  reason: string;
  evidence: RuleEvidence[];
}

/** 模型贡献（Top-K，rank_no 升序；正贡献推高分数，负贡献压低分数）。 */
export interface ModelContribution {
  feature_name: string;
  feature_value: number | null;
  contribution: number;
  direction: "up" | "down";
  rank_no: number;
}

/** 事件上下文：落库事实 + 原始 payload（"当时这条事件是什么样"）。 */
export interface EventContext {
  event?: {
    event_id: string;
    event_type: string;
    user_id: string | null;
    phone: string | null;
    device_id: string | null;
    device_fingerprint: Record<string, unknown>;
    ip: string | null;
    ip_region: string | null;
    address_hash: string | null;
    biz_no: string | null;
    occurred_at: string | null;
    source: string | null;
  };
  payload?: Record<string, unknown>;
}

/**
 * 证据焦点：案件**最近一次事件**的完整判据（三分数 + 命中规则 + 模型贡献 + 特征 + 上下文）。
 *
 * 后端只对最近一次事件组装证据，更早的事件由时间线呈现（点时间线不会换取证据，
 * 这是 P1 的口径；需要逐条回看时走决策详情接口）。
 */
export interface FocusEvidence {
  decision: {
    decision_id: string;
    event_id: string;
    rule_score: number;
    model_score: number;
    risk_score: number;
    risk_level: RiskLevel;
    action: DecisionAction;
    action_hint: string | null;
    decided_by: string;
    model_version: string | null;
    hit_count: number;
  } | null;
  hit_rules: HitRule[];
  model_contributions: ModelContribution[];
  features: FeatureGroup[];
  event_context: EventContext;
  feature_version: string | null;
  window_profile: string | null;
}

/** GET /cases/{case_no} 的 data —— 工作台中栏一次拿到的全部素材。 */
export interface CaseDetail {
  case: CaseRow;
  events: CaseTimelineItem[];
  decisions: CaseDecisionSummary[];
  profile: CaseProfile;
  biz_doc: BizDoc | null;
  graph: EntityGraphData;
  focus: FocusEvidence;
}

/** 处置提交体（POST /cases/{case_no}/dispose）。备注至少 10 字，后端兜底，前端同步校验。 */
export interface DisposePayload {
  business_result: BizResult;
  risk_actions: RiskAction[];
  remark: string;
}

/** 一项风控动作的执行结果（右栏联动结果逐项展示）。 */
export interface ActionExecItem {
  risk_action: string;
  exec_result: ExecResult;
  target_type: string | null;
  target_id: string | null;
  detail: Record<string, unknown> | null;
}

/** 处置响应 data。HTTP 恒 200：items 里的 skipped/failed 是业务结果，不是接口错误。 */
export interface DisposeResult {
  case: CaseRow;
  action_id: number;
  business_result: BizResult;
  risk_actions: RiskAction[];
  items: ActionExecItem[];
}

/** 批量归档里单条的结果。 */
export interface ArchiveItemResult {
  case_no: string;
  ok: boolean;
  reason: string | null;
}

export interface ArchiveResult {
  total: number;
  ok: number;
  results: ArchiveItemResult[];
}
