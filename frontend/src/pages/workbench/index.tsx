/**
 * 审核工作台页面（PRD §11.3）：三栏一体，不拆独立路由。
 *
 * 职责划分：
 * * 本页只做**状态持有与取数编排**（筛选条件、选中案件、三个查询、动作后失效）；
 * * 三栏的实现分别在 `components/workbench/` 下，左栏自管筛选交互，
 *   中栏/右栏各自只管"把详情渲染出来"与"提交处置"。
 *
 * 取数策略：
 * * 列表与详情各一个 TanStack Query，独立 key、独立失效；
 * * 任何动作（接手 / 处置 / 归档 / 关闭）后统一 `invalidateQueries` 两者的前缀，
 *   让左栏计数与右栏权限门禁都回到最新值 —— 列表 state_counts 与详情 status
 *   是同一份数据的两个投影，必须一起失效，否则会出现"列表说已处置、右栏还能提交"。
 * * 不做轮询：审核场景列表自动刷新会打断阅读，刷新交给左栏按钮与动作失效。
 */
import { useEffect, useState } from "react";
import { Button, Drawer, Space, Typography } from "antd";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiErrorMessage } from "@/api/client";
import { getCaseDetail, listCases } from "@/api/cases";
import CaseListPanel from "@/components/workbench/CaseListPanel";
import DisposalPanel from "@/components/workbench/DisposalPanel";
import EvidencePanel from "@/components/workbench/EvidencePanel";
import { LAYOUT } from "@/theme/layout";
import { useAuthStore } from "@/store/auth";
import type { CaseFilters } from "@/types/app";

const DEFAULT_FILTERS: CaseFilters = { page: 1, size: 20 };

export default function WorkbenchPage() {
  const queryClient = useQueryClient();
  const { user } = useAuthStore();
  const currentUserName = user?.username ?? null;
  const role = user?.role ?? "auditor";

  const [filters, setFilters] = useState<CaseFilters>(DEFAULT_FILTERS);
  const [selected, setSelected] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  // 折点口径与 DESIGN.md §4 一致（工作台三栏在 <1440px 视口下右栏收进 Drawer）。
  // 直接用 matchMedia 而不是 antd breakpoint：antd 的 lg 是 ≥1200、xl 是 ≥1600，
  // 而这里要的是 ≥1440 一个点，用 useMemo + 自定义阈值比凑 antd 的折点更清晰。
  const isNarrow = useViewportIsNarrow(LAYOUT.workbench.collapseAt);

  const listQuery = useQuery({
    queryKey: ["cases", filters],
    queryFn: () => listCases(filters),
    placeholderData: (previous) => previous,
  });

  const detailQuery = useQuery({
    queryKey: ["case-detail", selected],
    queryFn: () => getCaseDetail(selected as string),
    enabled: selected !== null,
  });

  // 首次进入 / 清空筛选时自动选中第一条：审核员的工作是从"列表第一条"开始的，
  // 而不是从"先看清怎么点"开始。已选中时切换筛选不强行改选（避免打断阅读）。
  const firstItem = listQuery.data?.items[0]?.case_no;
  useEffect(() => {
    if (selected === null && firstItem) setSelected(firstItem);
  }, [selected, firstItem]);

  /** 动作后的统一失效入口：列表与详情一起刷新。 */
  function invalidateCases() {
    void queryClient.invalidateQueries({ queryKey: ["cases"] });
    void queryClient.invalidateQueries({ queryKey: ["case-detail"] });
  }

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          marginBottom: 8,
        }}
      >
        <Typography.Title level={4} style={{ margin: 0 }}>
          审核工作台
        </Typography.Title>
        <Space size={8}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {role === "admin" ? "管理员模式：可直接处置未接手案件" : null}
          </Typography.Text>
          {/* 窄视口时右栏进入 Drawer，入口放这里避免遮挡中栏证据 */}
          {isNarrow ? (
            <Button size="small" onClick={() => setDrawerOpen(true)}>
              研判与处置
            </Button>
          ) : null}
        </Space>
      </div>

      <div style={{ flex: "1 1 auto", minHeight: 0, display: "flex", gap: LAYOUT.cardGap }}>
        <CaseListPanel
          filters={filters}
          onFiltersChange={(patch) => setFilters((prev) => ({ ...prev, ...patch }))}
          data={listQuery.data}
          isLoading={listQuery.isLoading}
          isError={listQuery.isError}
          errorMessage={apiErrorMessage(listQuery.error, "案件列表请求失败")}
          onRetry={() => listQuery.refetch()}
          selected={selected}
          onSelect={(caseNo) => setSelected(caseNo)}
          currentUserName={currentUserName}
        />

        <EvidencePanel
          detail={detailQuery.data}
          isLoading={detailQuery.isLoading}
          isError={detailQuery.isError}
          errorMessage={apiErrorMessage(detailQuery.error, "案件详情请求失败")}
          onRetry={() => detailQuery.refetch()}
          role={role}
          currentUserName={currentUserName}
          onChanged={invalidateCases}
        />

        {!isNarrow ? (
          <DisposalPanel
            detail={detailQuery.data}
            role={role}
            currentUserName={currentUserName}
            onChanged={invalidateCases}
          />
        ) : null}
      </div>

      <Drawer
        open={drawerOpen}
        width={LAYOUT.workbench.rightWidth}
        title="研判与处置"
        onClose={() => setDrawerOpen(false)}
        styles={{ body: { padding: 0 } }}
      >
        <div style={{ height: "100%", padding: 16 }}>
          <DisposalPanel
            detail={detailQuery.data}
            role={role}
            currentUserName={currentUserName}
            onChanged={invalidateCases}
          />
        </div>
      </Drawer>
    </div>
  );
}

/**
 * 视口宽度是否低于工作台三栏阈值（<1440px 时右栏收进 Drawer）。
 *
 * 用原生 `matchMedia` 而不是 antd 的 `Grid.useBreakpoint()`：
 * 后者把 xs/sm/md/lg/xl 五档全算出来，而这里只需要一个固定阈值。
 * 原生 API 更轻，也避免在"恰好等于 1440"这个边界上和 antd 的断点对不上。
 */
function useViewportIsNarrow(threshold: number): boolean {
  const [narrow, setNarrow] = useState<boolean>(() => window.innerWidth < threshold);
  useEffect(() => {
    const media = window.matchMedia(`(max-width: ${threshold - 1}px)`);
    const handler = (event: MediaQueryListEvent) => setNarrow(event.matches);
    setNarrow(media.matches);
    media.addEventListener("change", handler);
    return () => media.removeEventListener("change", handler);
  }, [threshold]);
  return narrow;
}
