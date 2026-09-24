/**
 * 语义标签组件集（DESIGN.md §6：风险等级 / 决策动作统一用 Tag + 语义色板）。
 *
 * 为什么值得单独抽出：这些标签在工作台的左栏列表、中栏证据、右栏处置、
 * 时间线里各出现一次以上。若每个页面各写一遍 `<Tag color=...>`，
 * 就必然出现"同一等级在列表是橙、在详情是黄"的漂移 —— 语义色一旦不一致，
 * 风控界面最核心的"一眼看风险"就失效了。
 *
 * 配色一律来自 theme（RISK_COLORS / ACTION_COLORS）或 constants/caseMeta，
 * 本文件不出现任何裸色值。
 */
import { Tag, Tooltip } from "antd";
import { ACTION_COLORS, RISK_COLORS } from "@/theme/semantic";
import {
  BIZ_STATUS_META,
  CASE_STATUS_META,
  EXEC_RESULT_META,
  LIST_TYPE_META,
} from "@/constants/caseMeta";
import type { CaseStatus, DecisionAction, ExecResult, RiskLevel } from "@/types/app";

interface RiskTagProps {
  level: RiskLevel;
  /** 追加分数展示（如 "中风险 62"）；列表里分数单独一列时可省略。 */
  score?: number | null;
  showScore?: boolean;
}

/** 风险等级标签：低 / 中 / 高风险（文案由 RISK_COLORS 统一提供，禁止各页面自造）。 */
export function RiskTag({ level, score, showScore = true }: RiskTagProps) {
  const meta = RISK_COLORS[level] ?? RISK_COLORS.low;
  return (
    <Tag style={{ color: meta.color, background: meta.bg, border: 0, marginInlineEnd: 0 }}>
      {meta.text}
      {showScore && score !== null && score !== undefined ? ` ${score}` : ""}
    </Tag>
  );
}

/** 决策动作标签：放行 / 挑战 / 人审 / 拦截。 */
export function ActionTag({ action }: { action: DecisionAction }) {
  const meta = ACTION_COLORS[action] ?? ACTION_COLORS.Pass;
  return (
    <Tag style={{ color: meta.color, background: meta.bg, border: 0, marginInlineEnd: 0 }}>
      {meta.text}
    </Tag>
  );
}

/** 案件状态标签（AntD 预设色，DESIGN.md §6 已固定 gold/blue/green/default）。 */
export function CaseStatusTag({ status }: { status: CaseStatus }) {
  const meta = CASE_STATUS_META[status];
  if (!meta) return <Tag>{status}</Tag>;
  return (
    <Tooltip title={meta.hint}>
      <Tag color={meta.tag} style={{ marginInlineEnd: 0 }}>
        {meta.label}
      </Tag>
    </Tooltip>
  );
}

/** 联动执行结果标签：已执行 / 无对象执行 / 需人工介入。 */
export function ExecResultTag({ result }: { result: ExecResult }) {
  const meta = EXEC_RESULT_META[result];
  if (!meta) return <Tag>{result}</Tag>;
  return (
    <Tag color={meta.tag} style={{ marginInlineEnd: 0 }}>
      {meta.label}
    </Tag>
  );
}

/** 名单类型标签：黑 / 白 / 灰。 */
export function ListTypeTag({ listType }: { listType: string }) {
  const meta = LIST_TYPE_META[listType];
  if (!meta) return <Tag>{listType}</Tag>;
  return (
    <Tag color={meta.tag} style={{ marginInlineEnd: 0 }}>
      {meta.label}
    </Tag>
  );
}

/** 业务单据状态标签（订单 / 退款单）。 */
export function BizStatusTag({ status }: { status: string | null | undefined }) {
  if (!status) return <Tag>-</Tag>;
  const meta = BIZ_STATUS_META[status];
  if (!meta) return <Tag>{status}</Tag>;
  return (
    <Tag color={meta.tag} style={{ marginInlineEnd: 0 }}>
      {meta.label}
    </Tag>
  );
}
