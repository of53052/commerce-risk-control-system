/**
 * DESIGN.md §4 / §5 — 布局尺寸常量。
 *
 * 尺寸全部从 DESIGN.md 抄下来，页面按字面常量引用（而不是凭感觉写 240px / 320px），
 * 以保证"工作台三栏"这类验收口径不会在页面之间漂移。
 */
export const LAYOUT = {
  siderWidth: 220,
  siderCollapsedWidth: 64,
  headerHeight: 56,
  contentPadding: 24,
  cardGap: 16,
  cardPadding: "16px 20px",
  workbench: {
    leftWidth: 320,
    centerMinWidth: 480,
    rightWidth: 400,
    collapseAt: 1440,
  },
  graph: { height: 420 },
} as const;