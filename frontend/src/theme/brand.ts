/**
 * DESIGN.md §2.5 / §2.2 — 深色导航与登录页 token。
 *
 * 品牌感只允许落在这两个位置（这是 DESIGN.md §9 的硬边界），
 * 其它页面不允许 import 这个文件 —— 如果出现 import 扩散，说明皮肤在失控。
 */
export const NAV_TOKENS = {
  bg: "#0B1220",
  text: "rgba(255,255,255,0.65)",
  textActive: "#FFFFFF",
  indicator: "#1E5EFF",
  indicatorWidth: 2,
  hoverBg: "rgba(30, 94, 255, 0.10)",
} as const;

export const LOGIN_TOKENS = {
  bg: "linear-gradient(135deg, #F8FAFC 0%, #EFF4FF 100%)",
  cardShadow: "0 20px 45px rgba(15,23,42,0.08)",
  cardRadius: 8,
  logoGradient: "linear-gradient(45deg, #1E5EFF 0%, #7C3AED 100%)",
} as const;

/** 品牌 Logo 渐变的唯一颜色对（DESIGN.md §2.1）。 */
export const BRAND_GRADIENT = {
  from: "#1E5EFF",
  to: "#7C3AED",
} as const;