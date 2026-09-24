/**
 * 关联实体图谱（DESIGN.md §6：ECharts graph 力导向，容器高度 420px，节点按风险等级取色，
 * 边按关系类型区分虚实）。
 *
 * 读图口径（与后端 `case_service._graph_payload` 一致，界面上要能自解释）：
 * * 中心节点 = 案件主体（描边用品牌蓝，尺寸固定 28）；
 * * 邻居节点 = **近 24 小时**内与主体共用设备 / IP / 手机号 / 收货地址的其它账号；
 * * 边只从中心连向邻居（不画邻居之间的边）—— 邻居之间的关系在力导向图里既能表达，
 *   也会让图迅速变成"一团毛线"，而审核员要回答的只是"谁和我这单是一家"。
 *
 * 空图（只有中心节点）不是错误：多数正常案件本来就没有关联账号，这里给出明确说明，
 * 避免审核员以为图谱坏了。
 */
import ReactECharts from "echarts-for-react";
import { Empty, Typography } from "antd";
import { RISK_COLORS } from "@/theme/semantic";
import { CHART_PALETTE } from "@/theme/charts";
import { LAYOUT } from "@/theme/layout";
import type { EntityGraphData } from "@/types/app";

/** 关系类型 → 展示名与线型（虚实区分：设备是硬证据，用实线；其余用不同虚线段）。 */
const RELATION_STYLE: Record<string, { label: string; type: "solid" | [number, number] }> = {
  device: { label: "同设备", type: "solid" },
  ip: { label: "同 IP", type: [4, 4] },
  phone: { label: "同手机号", type: [2, 4] },
  address: { label: "同收货地址", type: [8, 4] },
};

interface Props {
  data: EntityGraphData;
}

export default function EntityGraph({ data }: Props) {
  const nodes = data.nodes ?? [];
  const edges = data.edges ?? [];
  const neighborCount = nodes.filter((node) => !node.is_center).length;

  if (nodes.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="该主体没有可展示的事件记录，暂无图谱数据"
        style={{ padding: "32px 0" }}
      />
    );
  }

  // 邻居多时关掉常驻标签：30 个节点的标签会互相压盖，反而看不清中心是谁。
  const showLabels = nodes.length <= 20;

  const option = {
    color: CHART_PALETTE,
    tooltip: {
      confine: true,
      formatter: (params: { dataType: string; data: Record<string, unknown> }) => {
        if (params.dataType === "edge") return String(params.data.value ?? "");
        const item = params.data as {
          name: string;
          risk: string;
          case_cnt: number;
          is_center: boolean;
        };
        return [
          `<b>${item.name}</b>`,
          `风险等级：${item.risk}`,
          `历史案件数：${item.case_cnt}`,
          item.is_center ? "案件主体" : "关联账号",
        ].join("<br/>");
      },
    },
    series: [
      {
        type: "graph",
        layout: "force",
        roam: true,
        draggable: true,
        top: 8,
        bottom: 8,
        force: { repulsion: 260, edgeLength: 110, gravity: 0.06 },
        label: {
          show: showLabels,
          position: "bottom",
          fontSize: 10,
          color: "#475569",
          fontFamily: '"IBM Plex Mono", Consolas, monospace',
        },
        emphasis: { focus: "adjacency", label: { show: true } },
        data: nodes.map((node) => {
          const risk = RISK_COLORS[node.risk_level] ?? RISK_COLORS.low;
          return {
            id: node.id,
            name: node.label,
            symbolSize: node.size,
            risk: risk.text,
            case_cnt: node.case_cnt,
            is_center: node.is_center,
            itemStyle: {
              color: risk.color,
              opacity: node.is_center ? 1 : 0.82,
              borderColor: node.is_center ? "#1E5EFF" : "transparent",
              borderWidth: node.is_center ? 3 : 0,
            },
          };
        }),
        links: edges.map((edge) => {
          const style = RELATION_STYLE[edge.label] ?? { label: edge.label, type: "solid" as const };
          return {
            source: edge.source,
            target: edge.target,
            value: style.label,
            lineStyle: { type: style.type, width: 1.4, color: "#94A3B8", curveness: 0.12, opacity: 0.75 },
          };
        }),
      },
    ],
  };

  return (
    <div>
      <ReactECharts
        option={option}
        style={{ height: LAYOUT.graph.height }}
        notMerge
        lazyUpdate
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        节点颜色表示风险等级（中心为案件主体，描边为品牌蓝）；连线虚实表示关系类型。
        近 24 小时关联账号 {neighborCount} 个
        {neighborCount === 0 ? "，该主体暂未发现同设备 / 同 IP / 同地址的关联账号" : "。"}
      </Typography.Text>
    </div>
  );
}
