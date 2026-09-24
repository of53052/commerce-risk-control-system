/**
 * DESIGN.md §2.6 — ECharts 色板。
 *
 * 顺序即优先级：主序列用品牌蓝，风险序列用语义色，模型相关用紫色。
 * 这套色板只在 ECharts option 里被读取，页面不要直接引用。
 */
export const CHART_PALETTE = [
  "#1E5EFF", // brand-600 主序列
  "#16A34A", // 低风险
  "#D97706", // 中风险
  "#DC2626", // 高风险
  "#7C3AED", // 模型 / AI 点缀
  "#0EA5E9", // 信息
  "#475569", // 中性
];

/** SSE 实时滚屏顶条与大盘关键曲线的渐变（全站唯一允许的多色）。 */
export const SSE_GRADIENT = "linear-gradient(45deg, #1E5EFF 0%, #7C3AED 100%)";

export const CHART_THEME = {
  color: CHART_PALETTE,
  textStyle: { fontFamily: '"IBM Plex Mono", "JetBrains Mono", Consolas, monospace' },
  categoryAxis: { splitLine: { show: false } },
  valueAxis: { splitLine: { lineStyle: { color: "#F1F5F9" } } },
};