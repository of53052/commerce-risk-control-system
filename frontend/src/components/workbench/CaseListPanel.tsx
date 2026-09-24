/**
 * 工作台左栏：案件筛选 + 列表（PRD §11.3）。
 *
 * 三个刻意的取舍：
 * * **不用 Table**：320px 宽度下表格只能容纳 3 列，案件号、主体、状态必然被截断成省略号，
 *   点击目标也太小。这里用"卡片式行"，每个案件一行三排信息，一眼能看到
 *   案件号 / 主体 / 场景 / 风险 / 分数 / 命中数 / 状态 / 处理人 / 末次触发时间。
 * * **状态筛选标签带计数**：`status_counts` 与列表是同一次响应的产物（后端一个聚合查询），
 *   所以切换筛选前就能看到"待审 12 / 处理中 3"，不需要先点进去再发现是空的。
 * * **列表不做自动轮询**：审核场景里列表因后台新事件自己跳动会打断阅读；
 *   刷新交给右上角按钮与"接手/处置后失效重取"两条显式路径。
 */
import { Button, Empty, Input, Pagination, Result, Select, Skeleton, Space, Switch, Tag, Tooltip, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { CaseStatusTag, RiskTag } from "@/components/common/Tags";
import { CASE_STATUS_META, SCENE_LABEL } from "@/constants/caseMeta";
import { LAYOUT } from "@/theme/layout";
import { SURFACES } from "@/theme/tokens";
import { formatDateTime, formatRelative } from "@/utils/datetime";
import type { CaseFilters, CaseListResult, CaseRow, CaseScene, CaseStatus, RiskLevel } from "@/types/app";

/** 状态筛选的取值顺序：待办优先级从高到低（审核员最关心"待审"）。 */
const STATUS_OPTIONS: CaseStatus[] = ["pending", "processing", "disposed", "archived", "closed"];

const RISK_OPTIONS: { value: RiskLevel; label: string }[] = [
  { value: "high", label: "高风险" },
  { value: "mid", label: "中风险" },
  { value: "low", label: "低风险" },
];

const SCENE_OPTIONS: { value: CaseScene; label: string }[] = [
  { value: "order", label: "下单" },
  { value: "after_sale", label: "售后" },
  { value: "coupon", label: "领券" },
  { value: "login", label: "登录" },
];

interface Props {
  filters: CaseFilters;
  onFiltersChange: (patch: Partial<CaseFilters>) => void;
  data: CaseListResult | undefined;
  isLoading: boolean;
  isError: boolean;
  errorMessage: string;
  onRetry: () => void;
  selected: string | null;
  onSelect: (caseNo: string) => void;
  /** 当前登录用户名：用于在列表里标出"我接手的"，减少误点他人案件。 */
  currentUserName: string | null;
}

/** 单个案件行。抽出为子组件是为了让列表渲染逻辑与筛选逻辑分开读。 */
function CaseListItem({
  row,
  active,
  mine,
  onClick,
}: {
  row: CaseRow;
  active: boolean;
  mine: boolean;
  onClick: () => void;
}) {
  return (
    <div
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") onClick();
      }}
      style={{
        padding: "10px 12px",
        borderLeft: `2px solid ${active ? "#1E5EFF" : "transparent"}`,
        background: active ? SURFACES.selectedBg : "transparent",
        cursor: "pointer",
        borderBottom: "1px solid #F1F5F9",
      }}
    >
      <Space size={6} style={{ width: "100%", justifyContent: "space-between" }}>
        <span className="mono" style={{ fontSize: 12, color: "#0F172A" }}>
          {row.case_no}
        </span>
        <RiskTag level={row.risk_level} />
      </Space>

      <div style={{ marginTop: 4, fontSize: 12, color: "#475569" }}>
        <span className="mono">{row.subject_value}</span>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {" · "}
          {SCENE_LABEL[row.scene] ?? row.scene}
        </Typography.Text>
      </div>

      <div style={{ marginTop: 4, fontSize: 12, color: "#64748B", display: "flex", gap: 12 }}>
        <span>
          最高分 <span className="mono" style={{ color: "#0F172A" }}>{row.max_score}</span>
        </span>
        <span>
          命中 <span className="mono">{row.hit_cnt}</span>
        </span>
        <span>
          事件 <span className="mono">{row.event_cnt}</span>
        </span>
      </div>

      <Space size={6} style={{ marginTop: 6, width: "100%", justifyContent: "space-between" }}>
        <Space size={4}>
          <CaseStatusTag status={row.status} />
          {mine ? <Tag color="blue" style={{ marginInlineEnd: 0 }}>我的</Tag> : null}
        </Space>
        <Tooltip title={`${formatDateTime(row.last_at)}（末次触发）`}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {formatRelative(row.last_at)}
          </Typography.Text>
        </Tooltip>
      </Space>
    </div>
  );
}

export default function CaseListPanel({
  filters,
  onFiltersChange,
  data,
  isLoading,
  isError,
  errorMessage,
  onRetry,
  selected,
  onSelect,
  currentUserName,
}: Props) {
  const counts = data?.status_counts ?? {};
  const total = data?.total ?? 0;
  const items = data?.items ?? [];

  // "全部" 的数值用 total（当前筛选下命中数），状态项用各自的计数 ——
  // 两个数字口径不同，所以在选项文案上区分（全部用总数，状态用计数）。
  const statusOptions = [
    { value: "", label: `全部（${total}）` },
    ...STATUS_OPTIONS.map((status) => ({
      value: status,
      label: `${CASE_STATUS_META[status].label}（${counts[status] ?? 0}）`,
    })),
  ];

  return (
    <div
      style={{
        ...SURFACES.card,
        width: LAYOUT.workbench.leftWidth,
        flex: "0 0 auto",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        height: "100%",
      }}
    >
      <div style={{ padding: "12px 12px 8px", borderBottom: "1px solid #F1F5F9" }}>
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <Typography.Text strong>案件</Typography.Text>
          <Space size={4}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              共 {total} 条
            </Typography.Text>
            <Tooltip title="刷新列表">
              <Button size="small" type="text" icon={<ReloadOutlined />} onClick={onRetry} />
            </Tooltip>
          </Space>
        </Space>

        <Input
          allowClear
          size="small"
          placeholder="案件号 / 主体账号"
          style={{ marginTop: 8 }}
          onPressEnter={(event) => {
            onFiltersChange({ keyword: (event.target as HTMLInputElement).value, page: 1 });
          }}
          onBlur={(event) => {
            // 失焦也触发一次：审核员复制粘贴案件号后往往直接点列表，不会敲回车
            const value = (event.target as HTMLInputElement).value;
            if ((filters.keyword ?? "") !== value) onFiltersChange({ keyword: value, page: 1 });
          }}
        />

        <Select
          size="small"
          style={{ width: "100%", marginTop: 8 }}
          value={filters.status ?? ""}
          options={statusOptions}
          onChange={(value) =>
            onFiltersChange({ status: (value || undefined) as CaseStatus | undefined, page: 1 })
          }
        />

        <Space size={8} style={{ width: "100%", marginTop: 8 }}>
          <Select
            size="small"
            allowClear
            placeholder="风险等级"
            style={{ width: 108 }}
            value={filters.risk_level}
            options={RISK_OPTIONS}
            onChange={(value) => onFiltersChange({ risk_level: value, page: 1 })}
          />
          <Select
            size="small"
            allowClear
            placeholder="事件场景"
            style={{ width: 108 }}
            value={filters.scene}
            options={SCENE_OPTIONS}
            onChange={(value) => onFiltersChange({ scene: value, page: 1 })}
          />
        </Space>

        <Space size={6} style={{ marginTop: 8 }}>
          <Switch
            size="small"
            checked={Boolean(filters.mine)}
            onChange={(checked) => onFiltersChange({ mine: checked, page: 1 })}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            只看我接手的（{currentUserName ?? "-"}）
          </Typography.Text>
        </Space>
      </div>

      <div style={{ flex: "1 1 auto", overflowY: "auto" }}>
        {isError ? (
          <Result
            status="error"
            title="案件列表加载失败"
            subTitle={errorMessage}
            extra={
              <Button size="small" onClick={onRetry}>
                重试
              </Button>
            }
            style={{ padding: 16 }}
          />
        ) : isLoading ? (
          <div style={{ padding: 12 }}>
            <Skeleton active paragraph={{ rows: 6 }} title={false} />
          </div>
        ) : items.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="当前筛选下没有案件"
            style={{ padding: "32px 12px" }}
          >
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              可在「事件仿真」注入事件，或运行 scripts/gen_dataset.py 生成带场景的事件流
            </Typography.Text>
          </Empty>
        ) : (
          items.map((row) => (
            <CaseListItem
              key={row.case_no}
              row={row}
              active={row.case_no === selected}
              mine={Boolean(currentUserName) && row.handler === currentUserName}
              onClick={() => onSelect(row.case_no)}
            />
          ))
        )}
      </div>

      <div style={{ padding: "8px 12px", borderTop: "1px solid #F1F5F9" }}>
        <Pagination
          simple
          size="small"
          current={filters.page}
          pageSize={filters.size}
          total={total}
          onChange={(page) => onFiltersChange({ page })}
        />
      </div>
    </div>
  );
}
