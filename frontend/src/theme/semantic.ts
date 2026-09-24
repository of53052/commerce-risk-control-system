/**
 * DESIGN.md §2.3 — 风险与决策动作语义色。
 *
 * 唯一允许承载"风控语义"的颜色。页面、图表、图谱、徽标统一从这里取，
 * 禁止散落的硬编码（换肤或调语义时改一处即可全局生效）。
 */

export const RISK_COLORS = {
  low: { color: "#16A34A", bg: "#F0FDF4", text: "低风险" },
  mid: { color: "#D97706", bg: "#FFFBEB", text: "中风险" },
  high: { color: "#DC2626", bg: "#FEF2F2", text: "高风险" },
} as const;

export const ACTION_COLORS = {
  Pass: { color: "#16A34A", bg: "#F0FDF4", text: "放行" },
  Challenge: { color: "#0284C7", bg: "#F0F9FF", text: "挑战" },
  Review: { color: "#D97706", bg: "#FFFBEB", text: "人审" },
  Reject: { color: "#DC2626", bg: "#FEF2F2", text: "拦截" },
} as const;

/** 决策链路各环节（规则 / 名单 / 模型 / 决策）在回溯图、链路标签里使用。 */
export const LINK_COLORS = {
  feature: "#0EA5E9", // 特征：信息青
  list: "#0EA5E9",    // 名单：信息青
  rule: "#1E5EFF",    // 规则：品牌主色
  model: "#7C3AED",   // 模型：AI 点缀紫（DESIGN.md §2.2）
  decision: "inherit" // 决策：由结果决定（Pass/Challenge/Review/Reject）
} as const;

/**
 * 模型贡献的**方向**配色：正贡献推高风险、负贡献拉低风险。
 *
 * 这是"风险语义色"的一种用法，因此必须复用 `RISK_COLORS` 而不是另配一组红绿 ——
 * 否则界面里会出现两种含义不同却看起来一样的红色，审核员无法判断
 * "这个红是高风险等级，还是这一项在推高风险"（DESIGN.md §1.2：语义色唯一且稳定）。
 *
 * 同时导出底/字色，供条形图填充与数值文字共用，避免页面各取一半。
 */
export const RISK_DIRECTION = {
  /** 推高综合分：与高风险同色。 */
  positive: { color: RISK_COLORS.high.color, bg: RISK_COLORS.high.bg },
  /** 拉低综合分：与低风险同色。 */
  negative: { color: RISK_COLORS.low.color, bg: RISK_COLORS.low.bg },
} as const;

/** 窗口对比高亮底（"短窗集中"）：复用中风险底，表示"需要留意但未定性"。 */
export const WINDOW_HIGHLIGHT_BG = RISK_COLORS.mid.bg;
