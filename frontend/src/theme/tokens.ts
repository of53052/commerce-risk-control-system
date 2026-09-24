/**
 * DESIGN.md §2.1 / §2.3 / §2.4 / §10 — Ant Design 全局 Token。
 *
 * 设计原则：把 DESIGN.md 的色板收敛为 antd 的 token，而不是在页面里逐个写 `style`。
 * 这样"换主题"就只是一处改动，页面层永远不需要关心主色是什么。
 */
import type { ThemeConfig } from "antd";

export const tokens: ThemeConfig = {
  token: {
    colorPrimary: "#1E5EFF", // brand-600（全站主色）
    colorSuccess: "#16A34A", // 放行 / 处置成功
    colorWarning: "#F59E0B", // 中风险 / 待办
    colorError: "#DC2626",   // 高风险 / 拦截 / 删除
    colorInfo: "#0EA5E9",    // 普通提示（更轻、偏青，与主色拉开）

    colorTextBase: "#0F172A",   // neutral-900
    colorTextSecondary: "#475569",
    colorTextTertiary: "#64748B",
    colorTextQuaternary: "#94A3B8",

    colorBorder: "#E2E8F0",    // neutral-300
    colorBorderSecondary: "#F1F5F9", // neutral-200

    colorBgLayout: "#F6F7FB", // 页面背景（不是纯白，也不是死灰）
    colorBgContainer: "#FFFFFF",
    colorBgElevated: "#FFFFFF",
    colorBgSpotlight: "#0B1220", // Tooltip / 深色浮层

    borderRadius: 8,
    borderRadiusSM: 6,
    borderRadiusLG: 8,
    borderRadiusXS: 4,

    boxShadowTertiary: "0 1px 2px rgba(15,23,42,0.04)",
    boxShadowSecondary: "0 4px 16px rgba(15,23,42,0.08)",

    fontFamily:
      'Inter, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", -apple-system, "Segoe UI", sans-serif',
    fontFamilyCode: '"IBM Plex Mono", "JetBrains Mono", Consolas, monospace',
  },
  components: {
    Button: { controlHeight: 32, fontWeight: 500 },
    Card: { paddingLG: 20, boxShadowTertiary: "0 1px 2px rgba(15,23,42,0.04)" },
    Table: { headerBg: "#F8FAFC", headerSplitColor: "#F1F5F9" },
    Menu: { itemBorderRadius: 8 },
    Tag: { defaultBg: "#F1F5F9" },
    Statistic: { contentFontSize: 30 },
    Steps: { iconSize: 24 },
  },
};

/**
 * DESIGN.md §5 —— 阴影与圆角的裸值（与上面的 AntD token **必须一致**）。
 *
 * 为什么还要单独导出：工作台三栏是**自绘的分栏容器**（不是 AntD Card），
 * 页面里若直接写 `boxShadow: "0 1px 2px rgba(0,0,0,0.04)"` 就脱离了规范
 * （DESIGN 明确要求用带蓝相的 `rgba(15,23,42,*)`，且禁止叠加多层阴影）。
 * 页面引用本常量，规范调整时只改这里。
 */
export const SURFACES = {
  card: {
    background: "#FFFFFF",
    borderRadius: 8,
    boxShadow: "0 1px 2px rgba(15,23,42,0.04)",
    border: "1px solid #F1F5F9",
  },
  /** 选中项 / 强调底：brand-50（DESIGN.md §2.1）。 */
  selectedBg: "#EFF4FF",
} as const;
