/**
 * 工作台中栏：证据链与画像详情（PRD §11.3）。
 *
 * 信息顺序刻意固定为"人 → 事 → 数 → 网 → 判据 → 时间"：
 * 画像（这是谁）→ 业务单据（买的什么）→ 特征快照（行为像不像作弊）→
 * 关联图谱（还有谁）→ 命中规则与模型贡献（系统为什么这么判）→ 时间线（一共触发过几次）。
 * 前三块回答业务问题，后三块回答技术问题 —— 审核员从上往下读，不需要在页面里来回跳。
 *
 * 数据来源只有一个接口（`GET /cases/{case_no}`），因此这里不做任何二次取数，
 * 每块卡片都是纯渲染 + 空态兜底。
 */
import { useMutation } from "@tanstack/react-query";
import {
  App,
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Empty,
  Form,
  Input,
  Modal,
  Result,
  Skeleton,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useState, type CSSProperties } from "react";
import { apiErrorMessage } from "@/api/client";
import { archiveCase, claimCase, closeCase } from "@/api/cases";
import { ActionTag, BizStatusTag, CaseStatusTag, ListTypeTag, RiskTag } from "@/components/common/Tags";
import EntityGraph from "@/components/workbench/EntityGraph";
import {
  BIZ_RESULT_META,
  CUSTOMER_STATUS_META,
  DECIDED_BY_LABEL,
  LIST_DIMENSION_LABEL,
  SCENE_LABEL,
} from "@/constants/caseMeta";
import { AI_TOKENS } from "@/theme/ai";
import { LAYOUT } from "@/theme/layout";
import { RISK_DIRECTION, WINDOW_HIGHLIGHT_BG } from "@/theme/semantic";
import { SURFACES } from "@/theme/tokens";
import { formatAmount, formatDateTime, formatRelative, formatScore, formatSigned } from "@/utils/datetime";
import type {
  CaseDetail,
  CaseProfile,
  FeatureItem,
  FeatureGroup,
  HitRule,
  ModelContribution,
  Role,
} from "@/types/app";

interface Props {
  detail: CaseDetail | undefined;
  isLoading: boolean;
  isError: boolean;
  errorMessage: string;
  onRetry: () => void;
  role: Role;
  currentUserName: string | null;
  /** 案件状态变化（接手 / 归档 / 关闭）后，通知上层失效列表与详情查询。 */
  onChanged: () => void;
}

/** 三个分值的展示块（规则分 / 模型分 / 综合分）。 */
function ScoreBlock({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <div>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Typography.Text>
      <div className="mono" style={{ fontSize: 24, lineHeight: 1.2 }}>
        {formatScore(value)}
      </div>
    </div>
  );
}

/** 特征值渲染：整数不带小数位，小数保留 4 位（模型特征常有 4 位精度），空值统一 `-`。 */
function renderFeatureValue(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(4);
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/**
 * 窗口对比提示（PRD §11.3：对窗口对比做高亮，如 1h vs 24h 频次突增）。
 *
 * 判定口径：同一基础特征的 `_1h` 与 `_24h` 同时存在，且 `24h > 0` 时算 1h 占比；
 * 占比 ≥ 50% 说明"近一天的行为有一半挤在最后一小时"，这是薅羊毛脚本最典型的形态。
 * 阈值写死 0.5 而不是做成配置：这是**界面提示**而非决策依据，
 * 真正的判定归规则引擎（`RC_FREQ_*`），前端不引入第二个可配置的阈值来源。
 */
function windowHints(items: FeatureGroup["items"]): Record<string, string> {
  const values = new Map<string, number>();
  for (const item of items) {
    if (typeof item.value === "number") values.set(item.key, item.value);
  }
  const hints: Record<string, string> = {};
  for (const [key, value] of values) {
    if (!key.endsWith("_1h")) continue;
    const base = key.slice(0, -3);
    const day = values.get(`${base}_24h`);
    if (day === undefined || day <= 0) continue;
    const ratio = value / day;
    if (ratio >= 0.5 && value > 0) {
      hints[key] = `短窗集中：近 1 小时占近 24 小时的 ${(ratio * 100).toFixed(0)}%`;
    }
  }
  return hints;
}

/** 特征表的一行：特征本身 + 窗口对比提示（在表格里作为独立列展示）。 */
type FeatureRow = FeatureItem & { hint?: string };

/** 画像卡：账号年龄 / 渠道 / 名单状态 / 历史案件数 / 近 7 天行为。 */
function ProfileCard({ profile }: { profile: CaseProfile }) {
  const customer = profile.customer;
  const statusMeta = customer?.status ? CUSTOMER_STATUS_META[customer.status] : undefined;
  return (
    <Descriptions column={2} size="small" styles={{ label: { color: "#475569", width: 96 } }}>
      <Descriptions.Item label="主体账号">
        <span className="mono">{profile.subject_value}</span>
      </Descriptions.Item>
      <Descriptions.Item label="账号年龄">
        {profile.account_age_days === null ? "-" : `${profile.account_age_days} 天`}
      </Descriptions.Item>
      <Descriptions.Item label="注册渠道">{customer?.register_channel ?? "-"}</Descriptions.Item>
      <Descriptions.Item label="注册时间">{formatDateTime(customer?.register_at)}</Descriptions.Item>
      <Descriptions.Item label="业务状态">
        {statusMeta ? <Tag color={statusMeta.tag} style={{ marginInlineEnd: 0 }}>{statusMeta.label}</Tag> : "-"}
      </Descriptions.Item>
      <Descriptions.Item label="名单状态">
        {profile.list_status.length === 0 ? (
          <Tag style={{ marginInlineEnd: 0 }}>未命中名单</Tag>
        ) : (
          <Space size={4} wrap>
            {profile.list_status.map((entry, index) => (
              <Tooltip key={`${entry.list_type}-${entry.dimension}-${index}`} title={entry.reason ?? "无原因记录"}>
                <span>
                  <ListTypeTag listType={entry.list_type} />
                  <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>
                    {LIST_DIMENSION_LABEL[entry.dimension] ?? entry.dimension}
                  </Typography.Text>
                </span>
              </Tooltip>
            ))}
          </Space>
        )}
      </Descriptions.Item>
      <Descriptions.Item label="历史案件">
        <span className="mono">{profile.behavior.case_cnt_30d ?? 0}</span>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {" "}
          件 / 近 30 天
        </Typography.Text>
      </Descriptions.Item>
      <Descriptions.Item label="近 7 天行为">
        <span className="mono">{profile.behavior.event_cnt_7d ?? 0}</span>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {" "}
          次事件
        </Typography.Text>
      </Descriptions.Item>
    </Descriptions>
  );
}

/** 当前业务单据卡：订单或退款单（登录 / 领券场景为空）。 */
function BizDocCard({ doc }: { doc: CaseDetail["biz_doc"] }) {
  if (!doc) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="登录 / 领券场景没有业务单据，证据由画像、特征与图谱承担"
        style={{ padding: "16px 0" }}
      />
    );
  }

  if (doc.missing) {
    return (
      <Alert
        type="warning"
        showIcon
        message={doc.kind === "order" ? `订单 ${doc.order_no} 在业务表中不存在` : `退款单 ${doc.refund_no} 在业务表中不存在`}
        description="事件引用的单据与业务表不一致（数据异常）。处置联动会被记为「需人工介入」，需要人工核对业务库。"
      />
    );
  }

  if (doc.kind === "order") {
    return (
      <Descriptions column={2} size="small" styles={{ label: { color: "#475569", width: 96 } }}>
        <Descriptions.Item label="订单号">
          <span className="mono">{doc.order_no}</span>
        </Descriptions.Item>
        <Descriptions.Item label="状态">
          <BizStatusTag status={doc.status} />
        </Descriptions.Item>
        <Descriptions.Item label="商品">
          <span className="mono">{doc.product_id ?? "-"}</span>
        </Descriptions.Item>
        <Descriptions.Item label="数量">
          <span className="mono">{doc.quantity ?? "-"}</span>
        </Descriptions.Item>
        <Descriptions.Item label="金额">
          <span className="mono">{formatAmount(doc.amount)}</span>
        </Descriptions.Item>
        <Descriptions.Item label="下单账号">
          <span className="mono">{doc.user_id ?? "-"}</span>
        </Descriptions.Item>
      </Descriptions>
    );
  }

  return (
    <Descriptions column={2} size="small" styles={{ label: { color: "#475569", width: 96 } }}>
      <Descriptions.Item label="退款单号">
        <span className="mono">{doc.refund_no}</span>
      </Descriptions.Item>
      <Descriptions.Item label="状态">
        <BizStatusTag status={doc.status} />
      </Descriptions.Item>
      <Descriptions.Item label="退款金额">
        <span className="mono">{formatAmount(doc.refund_amount)}</span>
      </Descriptions.Item>
      <Descriptions.Item label="退款原因">{doc.reason ?? "-"}</Descriptions.Item>
      <Descriptions.Item label="关联订单">
        <span className="mono">{doc.order_no ?? "-"}</span>
      </Descriptions.Item>
      <Descriptions.Item label="订单金额">
        <span className="mono">{formatAmount(doc.order?.amount)}</span>
        {doc.order ? (
          <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
            订单状态 {doc.order.status}
          </Typography.Text>
        ) : null}
      </Descriptions.Item>
    </Descriptions>
  );
}

/** 特征快照：按 agg 分组折叠展示，短窗集中项高亮（窗口对比）。 */
function FeatureCard({ groups }: { groups: FeatureGroup[] }) {
  if (groups.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="没有特征快照（该事件可能未生成快照，或特征版本不匹配）"
        style={{ padding: "16px 0" }}
      />
    );
  }

  const items = groups.map((group) => {
    const hints = windowHints(group.items);
    const rows: FeatureRow[] = group.items.map((item) => ({ ...item, hint: hints[item.key] }));
    return {
      key: group.group,
      label: (
        <Space size={8}>
          <span>{group.group}</span>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {group.items.length} 项
          </Typography.Text>
        </Space>
      ),
      children: (
        <Table
          size="small"
          pagination={false}
          dataSource={rows}
          rowKey="key"
          // 高亮只加背景色，不加边框/角标：表格里同时出现多种强调会失真
          onRow={(record) => ({
            style: record.hint ? { background: WINDOW_HIGHLIGHT_BG } : undefined,
          })}
          columns={[
            {
              title: "特征",
              dataIndex: "key",
              width: 240,
              render: (value: string, record: FeatureRow) => (
                <Tooltip title={record.description || "（无描述）"}>
                  <span className="mono" style={{ fontSize: 12 }}>
                    {value}
                  </span>
                </Tooltip>
              ),
            },
            {
              title: "数值",
              dataIndex: "value",
              width: 120,
              render: (value: unknown) => (
                <span className="mono" style={{ fontSize: 12 }}>
                  {renderFeatureValue(value)}
                </span>
              ),
            },
            {
              title: "窗口对比",
              dataIndex: "hint",
              render: (hint: string | undefined) =>
                hint ? (
                  <Tag color="warning" style={{ marginInlineEnd: 0 }}>
                    {hint}
                  </Tag>
                ) : (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    -
                  </Typography.Text>
                ),
            },
          ]}
        />
      ),
    };
  });

  return <Collapse size="small" defaultActiveKey={groups.map((group) => group.group)} items={items} />;
}

/** 命中规则明细：每条规则可展开看条件级证据（哪个字段、什么比较、实际值多少）。 */
function HitRulesCard({ rules }: { rules: HitRule[] }) {
  if (rules.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="本次决策没有命中任何规则（结论由模型或名单给出）"
        style={{ padding: "16px 0" }}
      />
    );
  }
  return (
    <Table
      size="small"
      pagination={false}
      dataSource={rules}
      rowKey="rule_code"
      expandable={{
        rowExpandable: (record) => record.evidence.length > 0,
        expandedRowRender: (record) => (
          <Table
            size="small"
            pagination={false}
            rowKey={(item) => `${item.field}-${item.op}-${String(item.expected)}`}
            dataSource={record.evidence}
            columns={[
              { title: "字段", dataIndex: "field", render: (value: string) => <span className="mono" style={{ fontSize: 12 }}>{value}</span> },
              { title: "比较", dataIndex: "op", width: 64 },
              { title: "阈值", dataIndex: "expected", width: 96, render: (value: unknown) => <span className="mono" style={{ fontSize: 12 }}>{renderFeatureValue(value)}</span> },
              { title: "实际", dataIndex: "actual", width: 96, render: (value: unknown) => <span className="mono" style={{ fontSize: 12 }}>{renderFeatureValue(value)}</span> },
              {
                title: "结果",
                dataIndex: "passed",
                width: 88,
                render: (passed: boolean, item) =>
                  item.missing ? (
                    <Tag color="default" style={{ marginInlineEnd: 0 }}>字段缺失</Tag>
                  ) : passed ? (
                    <Tag color="error" style={{ marginInlineEnd: 0 }}>命中</Tag>
                  ) : (
                    <Tag style={{ marginInlineEnd: 0 }}>未命中</Tag>
                  ),
              },
            ]}
          />
        ),
      }}
      columns={[
        {
          title: "规则",
          dataIndex: "rule_name",
          render: (name: string, record) => (
            <Space direction="vertical" size={0}>
              <span>{name}</span>
              <Typography.Text type="secondary" style={{ fontSize: 12 }} className="mono">
                {record.rule_code}
              </Typography.Text>
            </Space>
          ),
        },
        { title: "分类", dataIndex: "rule_category", width: 96 },
        {
          title: "分值",
          dataIndex: "score",
          width: 80,
          render: (value: number) => (
            <span className="mono" style={{ color: RISK_DIRECTION.positive.color }}>+{value}</span>
          ),
        },
        {
          title: "命中原因",
          dataIndex: "reason",
          render: (reason: string) => (
            <Tooltip title={reason}>
              <Typography.Text style={{ fontSize: 12 }} ellipsis>
                {reason}
              </Typography.Text>
            </Tooltip>
          ),
        },
      ]}
    />
  );
}

/**
 * 模型贡献 Top-K 条形图（DESIGN.md §6/§9：标题带 ai-600 色条，正贡献红、负贡献绿）。
 *
 * 颜色方向与"风险高低"相反（正贡献推高风险分 → 红），所以这里**不能**复用
 * RISK_COLORS：它表达的是等级，而这里表达的是"对分数的推动方向"。
 */
function ModelContributionCard({ contributions }: { contributions: ModelContribution[] }) {
  if (contributions.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="模型未参与本次决策（名单直接裁定，或模型未启用）"
        style={{ padding: "16px 0" }}
      />
    );
  }
  const rows = [...contributions].sort((a, b) => a.rank_no - b.rank_no);
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      {rows.map((item) => {
        const positive = item.contribution >= 0;
        // 条形长度按"同组内最大绝对值"归一化：模型的贡献量级差异很大，
        // 用绝对分数画条形会让 Top1 占满、其余全成一条细线。
        const max = Math.max(...rows.map((row) => Math.abs(row.contribution)), 0.0001);
        const width = `${Math.min(100, (Math.abs(item.contribution) / max) * 100).toFixed(1)}%`;
        return (
          <div key={`${item.rank_no}-${item.feature_name}`}>
            <Space size={8} style={{ width: "100%", justifyContent: "space-between" }}>
              <span className="mono" style={{ fontSize: 12 }}>
                {item.feature_name}
              </span>
              <span
                className="mono"
                style={{ fontSize: 12, color: positive ? RISK_DIRECTION.positive.color : RISK_DIRECTION.negative.color }}
              >
                {formatSigned(item.contribution)}
              </span>
            </Space>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 2 }}>
              <div style={{ flex: "1 1 auto", height: 6, background: "#F1F5F9", borderRadius: 3 }}>
                <div
                  style={{
                    width,
                    height: 6,
                    borderRadius: 3,
                    background: positive ? RISK_DIRECTION.positive.color : RISK_DIRECTION.negative.color,
                  }}
                />
              </div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                特征值 {renderFeatureValue(item.feature_value)}
              </Typography.Text>
            </div>
          </div>
        );
      })}
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        正贡献推高风险分（红），负贡献压低风险分（绿）；条形长度按本次贡献绝对值归一化。
      </Typography.Text>
    </Space>
  );
}

/**
 * 案件事件时间线。
 *
 * 时间线只做"这件事一共被触发过几次"的呈现：证据区永远展示**最近一次**事件的判据
 * （后端 `focus` 就这样返回）。不做"点击历史事件切换证据"是刻意的 ——
 * 那需要后端按 event_id 组装第二份证据，而 P1 的口径是"先让审核员在最近一次证据上闭环"，
 * 历史事件要逐条回看时走决策详情接口。
 */
function TimelineCard({
  events,
  focusDecisionId,
}: {
  events: CaseDetail["events"];
  focusDecisionId: string | null;
}) {
  if (events.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="没有关联事件（案件仅由一次决策生成，且该事件已被清理）"
        style={{ padding: "16px 0" }}
      />
    );
  }
  return (
    <Table
      size="small"
      pagination={false}
      dataSource={events}
      rowKey="event_id"
      onRow={(record) => ({
        style: record.decision_id === focusDecisionId ? { background: SURFACES.selectedBg } : undefined,
      })}
      columns={[
        {
          title: "时间",
          dataIndex: "occurred_at",
          width: 160,
          render: (value: string) => (
            <Tooltip title={formatRelative(value)}>
              <span className="mono" style={{ fontSize: 12 }}>
                {formatDateTime(value)}
              </span>
            </Tooltip>
          ),
        },
        { title: "事件", dataIndex: "event_type", width: 132, render: (value: string) => <span className="mono" style={{ fontSize: 12 }}>{value}</span> },
        { title: "场景", dataIndex: "scene", width: 72, render: (value: string) => SCENE_LABEL[value as keyof typeof SCENE_LABEL] ?? value },
        { title: "决策", dataIndex: "action", width: 88, render: (value: CaseDetail["events"][number]["action"]) => <ActionTag action={value} /> },
        {
          title: "风险分",
          dataIndex: "risk_score",
          width: 96,
          render: (value: number, record) => (
            <Space size={4}>
              <span className="mono" style={{ fontSize: 12 }}>{formatScore(value, 0)}</span>
              <RiskTag level={record.risk_level} showScore={false} />
            </Space>
          ),
        },
        { title: "命中", dataIndex: "hit_count", width: 56, render: (value: number) => <span className="mono" style={{ fontSize: 12 }}>{value}</span> },
        {
          title: "业务单号",
          dataIndex: "biz_no",
          render: (value: string | null) => (
            <span className="mono" style={{ fontSize: 12 }}>{value ?? "-"}</span>
          ),
        },
      ]}
    />
  );
}

/** 事件上下文：落库事实 + 原始 payload（技术排查用，默认折叠）。 */
function EventContextCard({ context }: { context: CaseDetail["focus"]["event_context"] }) {
  const event = context.event;
  if (!event) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="没有事件上下文（该决策的事件记录缺失）"
        style={{ padding: "16px 0" }}
      />
    );
  }
  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Descriptions column={2} size="small" styles={{ label: { color: "#475569", width: 88 } }}>
        <Descriptions.Item label="事件 ID">
          <span className="mono" style={{ fontSize: 12 }}>{event.event_id}</span>
        </Descriptions.Item>
        <Descriptions.Item label="来源">
          <Tag style={{ marginInlineEnd: 0 }}>{event.source ?? "-"}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="设备号">
          <span className="mono" style={{ fontSize: 12 }}>{event.device_id ?? "-"}</span>
        </Descriptions.Item>
        <Descriptions.Item label="设备指纹">
          <span className="mono" style={{ fontSize: 12 }}>
            {Object.entries(event.device_fingerprint ?? {})
              .map(([key, value]) => `${key}=${renderFeatureValue(value)}`)
              .join(" / ") || "-"}
          </span>
        </Descriptions.Item>
        <Descriptions.Item label="IP / 归属">
          <span className="mono" style={{ fontSize: 12 }}>
            {event.ip ?? "-"}
            {event.ip_region ? `（${event.ip_region}）` : ""}
          </span>
        </Descriptions.Item>
        <Descriptions.Item label="收货地址">
          <span className="mono" style={{ fontSize: 12 }}>{event.address_hash ?? "-"}</span>
        </Descriptions.Item>
      </Descriptions>
      <Collapse
        size="small"
        items={[
          {
            key: "payload",
            label: "原始事件 payload",
            children: (
              <pre
                className="mono"
                style={{
                  margin: 0,
                  padding: 12,
                  background: "#F8FAFC",
                  borderRadius: 6,
                  fontSize: 12,
                  maxHeight: 260,
                  overflow: "auto",
                }}
              >
                {JSON.stringify(context.payload ?? {}, null, 2)}
              </pre>
            ),
          },
        ]}
      />
    </Space>
  );
}

/**
 * 案件头部：摘要信息 + 案件级动作（接手 / 归档 / 强制关闭）。
 *
 * 按钮可见性按"角色 + 状态"共同决定（PRD §4.2 权限矩阵在**服务端**执行，
 * 这里只是不把明显的非法操作摆到界面上；即使前端被绕过，后端仍会返回 403/409）：
 * * 接手：auditor / admin，且案件为 pending；
 * * 归档：admin，且案件为 disposed（未处置不能归档，避免"没看就归档"）；
 * * 强制关闭：admin，且案件未结案（pending / processing），必须填原因。
 */
function CaseHeader({
  row,
  role,
  currentUserName,
  onChanged,
}: {
  row: CaseDetail["case"];
  role: Role;
  currentUserName: string | null;
  onChanged: () => void;
}) {
  const [closeVisible, setCloseVisible] = useState(false);
  const [closeForm] = Form.useForm<{ reason: string }>();
  // 用 App.useApp() 取 message 实例，而不是 `antd` 的静态方法：
  // 静态方法拿不到 ConfigProvider 的主题上下文，会退回默认蓝色主色（与 DESIGN.md 不一致）。
  const { message } = App.useApp();

  const claim = useMutation({
    mutationFn: (caseNo: string) => claimCase(caseNo),
    onSuccess: () => {
      message.success(`已接手案件 ${row.case_no}，处置区已解锁`);
      onChanged();
    },
    onError: (error: unknown) => message.error(apiErrorMessage(error, "接手失败")),
  });

  const archive = useMutation({
    mutationFn: (caseNo: string) => archiveCase(caseNo),
    onSuccess: (data) => {
      if (data.result.ok) message.success(`已归档案件 ${data.result.case_no}`);
      else message.warning(data.result.reason ?? "归档未执行：当前状态不允许");
      onChanged();
    },
    onError: (error: unknown) => message.error(apiErrorMessage(error, "归档失败")),
  });

  const close = useMutation({
    mutationFn: ({ caseNo, reason }: { caseNo: string; reason: string }) => closeCase(caseNo, reason),
    onSuccess: () => {
      message.success(`已强制关闭案件 ${row.case_no}`);
      setCloseVisible(false);
      closeForm.resetFields();
      onChanged();
    },
    onError: (error: unknown) => message.error(apiErrorMessage(error, "关闭失败")),
  });

  const canClaim = (role === "auditor" || role === "admin") && row.status === "pending";
  const canArchive = role === "admin" && row.status === "disposed";
  const canClose = role === "admin" && (row.status === "pending" || row.status === "processing");
  const mine = Boolean(currentUserName) && row.handler === currentUserName;
  const disposeLabel = row.dispose_result ? BIZ_RESULT_META[row.dispose_result]?.label ?? row.dispose_result : null;

  return (
    <div style={{ padding: "12px 16px", borderBottom: "1px solid #F1F5F9" }}>
      <Space size={12} style={{ width: "100%", justifyContent: "space-between", flexWrap: "wrap" }}>
        <Space size={8} wrap>
          <Typography.Text strong className="mono" style={{ fontSize: 14 }}>
            {row.case_no}
          </Typography.Text>
          <RiskTag level={row.risk_level} score={row.max_score} />
          <CaseStatusTag status={row.status} />
          <Tag style={{ marginInlineEnd: 0 }}>{SCENE_LABEL[row.scene] ?? row.scene}</Tag>
          {mine && row.status === "processing" ? (
            <Tag color="blue" style={{ marginInlineEnd: 0 }}>我处理中</Tag>
          ) : null}
        </Space>

        <Space size={8}>
          {canClaim ? (
            <Button
              type="primary"
              size="small"
              loading={claim.isPending}
              onClick={() => claim.mutate(row.case_no)}
            >
              接手案件
            </Button>
          ) : null}
          {canArchive ? (
            <Button size="small" loading={archive.isPending} onClick={() => archive.mutate(row.case_no)}>
              归档
            </Button>
          ) : null}
          {/* 危险动作与常规动作留出 24px 视觉间距（DESIGN.md §5 行为动线） */}
          {canClose ? (
            <span style={{ marginInlineStart: 16 }}>
              <Button danger size="small" onClick={() => setCloseVisible(true)}>
                强制关闭
              </Button>
            </span>
          ) : null}
        </Space>
      </Space>

      <div style={{ marginTop: 6, fontSize: 12, color: "#64748B" }}>
        <span className="mono">{row.subject_value}</span>
        {` · 末次触发 ${formatDateTime(row.last_at)} · 关联事件 ${row.event_cnt} 次 · 累计命中 ${row.hit_cnt} 条`}
        {row.handler ? ` · 处理人 ${row.handler}` : " · 尚未接手"}
        {disposeLabel ? ` · 处置结论 ${disposeLabel}` : ""}
        {row.close_reason ? ` · 关闭原因 ${row.close_reason}` : ""}
      </div>

      <Modal
        title="强制关闭案件"
        open={closeVisible}
        okText="确认关闭"
        cancelText="取消"
        okButtonProps={{ danger: true, loading: close.isPending }}
        onCancel={() => setCloseVisible(false)}
        onOk={() => {
          closeForm
            .validateFields()
            .then((values) => close.mutate({ caseNo: row.case_no, reason: values.reason }))
            .catch(() => undefined);
        }}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="强制关闭是不可逆的兜底操作"
          description="关闭只用于异常场景（重复案件、误报建案）。关闭后该案件不再参与合案，也不会再出现在待办里，操作会记入审计日志。"
        />
        <Form form={closeForm} layout="vertical" requiredMark={false}>
          <Form.Item
            name="reason"
            label="关闭原因"
            rules={[{ required: true, min: 3, max: 255, message: "请填写关闭原因（至少 3 个字）" }]}
          >
            <Input.TextArea rows={3} placeholder="例如：同一事件的重复建案，已合并到 C2026..." />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

/**
 * 中栏容器：加载 / 错误 / 空态 + 六块证据卡片。
 *
 * 卡片顺序见文件头注释。这里只做"状态分发 + 组装"，所有取数与动作在子组件里，
 * 因此新增一块证据（例如 P2 的"策略命中历史"）时只需在这里插一张 Card。
 */
export default function EvidencePanel({
  detail,
  isLoading,
  isError,
  errorMessage,
  onRetry,
  role,
  currentUserName,
  onChanged,
}: Props) {
  const container: CSSProperties = {
    ...SURFACES.card,
    flex: "1 1 auto",
    minWidth: LAYOUT.workbench.centerMinWidth,
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
    height: "100%",
  };

  if (isError) {
    return (
      <div style={container}>
        <Result
          status="error"
          title="案件详情加载失败"
          subTitle={errorMessage}
          extra={
            <Button size="small" onClick={onRetry}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  if (isLoading) {
    return (
      <div style={container}>
        <div style={{ padding: 16 }}>
          <Skeleton active avatar paragraph={{ rows: 3 }} />
          <Skeleton active paragraph={{ rows: 6 }} style={{ marginTop: 16 }} />
          <Skeleton active paragraph={{ rows: 4 }} style={{ marginTop: 16 }} />
        </div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div style={container}>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="从左侧选择一个案件开始审核"
          style={{ margin: "auto" }}
        >
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            左边列表按状态与风险等级筛选；点击任意一行即可加载画像、特征、图谱与判据
          </Typography.Text>
        </Empty>
      </div>
    );
  }

  const focus = detail.focus;
  const decision = focus.decision;

  return (
    <div style={container}>
      <CaseHeader row={detail.case} role={role} currentUserName={currentUserName} onChanged={onChanged} />

      <div
        style={{
          flex: "1 1 auto",
          overflowY: "auto",
          padding: 16,
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <Card size="small" title="用户画像" styles={{ body: { paddingTop: 12 } }}>
          <ProfileCard profile={detail.profile} />
        </Card>

        <Card size="small" title="当前业务单据" styles={{ body: { paddingTop: 12 } }}>
          <BizDocCard doc={detail.biz_doc} />
        </Card>

        <Card
          size="small"
          title="实时特征快照"
          extra={
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              窗口 {focus.window_profile ?? "-"} · 特征版本 {focus.feature_version ?? "-"}
            </Typography.Text>
          }
        >
          <FeatureCard groups={focus.features} />
        </Card>

        <Card size="small" title="关联实体图谱">
          <EntityGraph data={detail.graph} />
        </Card>

        <Card size="small" title="命中规则与模型判据">
          <Space size={32} style={{ marginBottom: 12 }} wrap>
            <ScoreBlock label="规则分" value={decision?.rule_score} />
            <ScoreBlock label="模型分" value={decision?.model_score} />
            <ScoreBlock label="综合分" value={decision?.risk_score} />
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                决策来源
              </Typography.Text>
              <div>
                <Tag style={{ marginInlineEnd: 0 }}>
                  {decision ? DECIDED_BY_LABEL[decision.decided_by] ?? decision.decided_by : "-"}
                </Tag>
              </div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                模型版本 {decision?.model_version ?? "-"} · 命中 {decision?.hit_count ?? 0} 条规则
              </Typography.Text>
            </div>
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                决策动作
              </Typography.Text>
              <div>{decision ? <ActionTag action={decision.action} /> : "-"}</div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                决策号 {decision?.decision_id ?? "-"}
              </Typography.Text>
            </div>
          </Space>

          <HitRulesCard rules={focus.hit_rules} />

          {/* 模型关注点：唯一允许使用智能感配色的业务区块（DESIGN.md §9） */}
          <div
            style={{
              marginTop: 16,
              padding: "12px 12px 12px 14px",
              background: AI_TOKENS.bg,
              borderLeft: `3px solid ${AI_TOKENS.color}`,
              borderRadius: 6,
            }}
          >
            <Space size={8} style={{ marginBottom: 8 }}>
              <Typography.Text strong style={{ color: AI_TOKENS.color }}>
                模型关注点 Top{focus.model_contributions.length || 0}
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                模型版本 {decision?.model_version ?? "-"}
              </Typography.Text>
            </Space>
            <ModelContributionCard contributions={focus.model_contributions} />
          </div>
        </Card>

        <Card size="small" title="案件事件时间线">
          <TimelineCard events={detail.events} focusDecisionId={decision?.decision_id ?? null} />
        </Card>

        <Card size="small" title="事件上下文">
          <EventContextCard context={focus.event_context} />
        </Card>
      </div>
    </div>
  );
}
