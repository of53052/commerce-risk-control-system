/**
 * 案件域文案与标签元数据（DESIGN.md §6「组件使用约定」的落点之一）。
 *
 * 分工原则：
 * * **颜色**只从 `theme/semantic.ts` 取（风险等级 / 决策动作是业务语义色，禁止硬编码）；
 * * **文案与 AntD 预设标签色**放这里 —— 状态、场景、处置结论、风控动作、联动结果
 *   这些是"业务字典"，会随着需求增加条目，散在各个组件里会前后不一致。
 *
 * 所有取值与后端常量一一对应（`app/models/case.py`、`app/models/biz.py`、
 * `app/models/rclist.py`）。后端新增状态或动作时，**必须同时在这里补齐**，
 * 否则界面会退化成"显示原始英文枚举值"。
 */
import type { BizResult, CaseScene, CaseStatus, DecisionAction, ExecResult, RiskAction } from "@/types/app";

/** 事件场景（PRD §6.1 五类事件归并到四个场景）。 */
export const SCENE_LABEL: Record<CaseScene, string> = {
  login: "登录",
  coupon: "领券",
  order: "下单",
  after_sale: "售后",
};

/** 案件状态标签（DESIGN.md §6：待审 gold / 审核中 blue / 已处置 green / 已归档 default / 已关闭 default）。 */
export const CASE_STATUS_META: Record<CaseStatus, { label: string; tag: string; hint: string }> = {
  pending: { label: "待审", tag: "gold", hint: "等待审核员接手" },
  processing: { label: "审核中", tag: "blue", hint: "已被接手，证据已锁定归属人" },
  disposed: { label: "已处置", tag: "green", hint: "结论已提交，联动已执行" },
  archived: { label: "已归档", tag: "default", hint: "归档后不再参与合案" },
  closed: { label: "已关闭", tag: "default", hint: "管理员强制关闭（异常兜底）" },
};

/** 业务结论（单选）。effect 是右栏选项下的副作用说明，直接告诉审核员"选了会发生什么"。 */
export const BIZ_RESULT_META: Record<BizResult, { label: string; tag: string; effect: string }> = {
  approve: { label: "通过", tag: "success", effect: "业务正常继续：订单保留 / 退款通过" },
  reject: { label: "驳回", tag: "error", effect: "业务终止：取消未支付订单 / 驳回退款申请" },
};

/**
 * 风控动作（多选）。
 *
 * `listEffect` 非空时，右栏会提示"本次处置将写入名单库：xxx"——
 * 名单写入是**跨案件生效**的动作（下一次决策就会用上），必须让操作者提前看到，
 * 而不是提交后在别处发现。
 */
export const RISK_ACTION_META: Record<
  RiskAction,
  { label: string; tag: string; effect: string; listEffect: string | null }
> = {
  pass: {
    label: "不追加措施",
    tag: "default",
    effect: "只记录处置结论，不改动任何业务单据或名单",
    listEffect: null,
  },
  block_order: {
    label: "拦截关联订单",
    tag: "volcano",
    effect: "案件证据链上的未支付订单置为已取消；已支付订单只记录「需人工退款」",
    listEffect: null,
  },
  blacklist_user: {
    label: "拉黑账号",
    tag: "red",
    effect: "写入黑名单（user）并把业务用户置为黑名单状态",
    listEffect: "黑名单 · 账号",
  },
  ban_device: {
    label: "封禁设备",
    tag: "magenta",
    effect: "把案件证据链上最近的设备写入黑名单（device）；事件无设备号时跳过",
    listEffect: "黑名单 · 设备",
  },
  watchlist_add: {
    label: "加入灰名单",
    tag: "gold",
    effect: "写入灰名单（user）继续观察，不阻断当前业务",
    listEffect: "灰名单 · 账号",
  },
};

/** 风控动作在界面上的展示顺序（与后端动作常量顺序一致，避免"按勾选顺序回显"时读起来跳跃）。 */
export const RISK_ACTION_ORDER: RiskAction[] = [
  "block_order",
  "blacklist_user",
  "ban_device",
  "watchlist_add",
  "pass",
];

/** 五种动作的短标签（用于联动结果、时间线等窄空间）；`reject_refund` 是后端在售后驳回时补记的项。 */
export const RISK_ACTION_LABEL: Record<string, string> = {
  ...Object.fromEntries(RISK_ACTION_ORDER.map((key) => [key, RISK_ACTION_META[key].label])),
  reject_refund: "驳回退款申请",
};

/** 联动执行结果三档（PRD §9.4）。skipped 是预期内状态，failed 才需要人工介入。 */
export const EXEC_RESULT_META: Record<ExecResult, { label: string; tag: string }> = {
  success: { label: "已执行", tag: "success" },
  skipped: { label: "无对象执行", tag: "default" },
  failed: { label: "需人工介入", tag: "error" },
};

/** 决策动作短文案（共用后端 DecisionAction 类型；流转到策略页的决策链路也用它）。 */
export const DECISION_LABEL: Record<DecisionAction, string> = {
  Pass: "放行",
  Challenge: "挑战",
  Review: "人审",
  Reject: "拦截",
};

/** 决策来源（PRD §8.4 融合与仲裁）。名单结论优先级最高，规则 + 模型次之。 */
export const DECIDED_BY_LABEL: Record<string, string> = {
  list: "名单优先",
  rule_model: "规则 + 模型融合",
};

/** 名单类型 → 标签（`app/models/rclist.py`）。 */
export const LIST_TYPE_META: Record<string, { label: string; tag: string }> = {
  black: { label: "黑名单", tag: "red" },
  white: { label: "白名单", tag: "success" },
  gray: { label: "灰名单", tag: "gold" },
};

/** 名单维度 → 文案。 */
export const LIST_DIMENSION_LABEL: Record<string, string> = {
  user: "账号",
  phone: "手机号",
  ip: "IP",
  device: "设备",
  address: "收货地址",
};

/** 业务单据状态（`app/models/biz.py`）。工单类状态给 AntD 预设标签色即可，没有业务语义。 */
export const BIZ_STATUS_META: Record<string, { label: string; tag: string }> = {
  created: { label: "已创建", tag: "default" },
  paid: { label: "已支付", tag: "processing" },
  cancelled: { label: "已取消", tag: "default" },
  refunded: { label: "已退款", tag: "success" },
  applied: { label: "退款申请中", tag: "gold" },
  approved: { label: "退款已同意", tag: "success" },
  rejected: { label: "退款已驳回", tag: "error" },
};

/** 账号业务状态（`biz_customer.status`）。blacklisted 与 RISK_COLORS.high 同系但属业务字段，用常量字典更稳。 */
export const CUSTOMER_STATUS_META: Record<string, { label: string; tag: string }> = {
  normal: { label: "正常", tag: "success" },
  blacklisted: { label: "已拉黑", tag: "error" },
};
