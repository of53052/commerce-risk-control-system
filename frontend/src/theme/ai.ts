/**
 * DESIGN.md §2.2 / §9 — "智能感"统一入口。
 *
 * 只允许在模型相关位置使用：模型页、链路回溯、模型贡献 Top5、SSE 滚屏。
 * 其它地方见到这个 token 被 import，说明智能感已经溢出 —— 请回头检查 §9 边界。
 */
export const AI_TOKENS = {
  color: "#7C3AED",    // ai-600
  bg: "#F5F3FF",       // ai-50
  glow: "radial-gradient(closest-side, rgba(124,58,237,0.12), transparent)",
  bar: "linear-gradient(90deg, #7C3AED 0%, rgba(124,58,237,0) 100%)",
} as const;