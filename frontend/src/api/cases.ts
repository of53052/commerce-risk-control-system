/**
 * 案件接口封装（后端 `app/api/cases.py`）。
 *
 * 设计取舍：
 * * **详情只发一个请求**：`GET /cases/{case_no}` 已经把画像 / 单据 / 特征分组 /
 *   图谱 / 证据（命中规则 + 模型贡献）/ 时间线一次返回。前端不做"多接口拼装"，
 *   否则任何一次网络抖动都会让工作台出现"画像有、图谱空白"的半成品状态。
 * * **联动失败不是接口错误**：`disposeCase` 正常 resolve，失败项在 `items` 里
 *   以 `exec_result=failed/skipped` 表达（PRD §9.4），界面逐项展示而不是弹全局错误。
 */
import { api } from "./client";
import type {
  ApiOk,
  ArchiveItemResult,
  ArchiveResult,
  CaseDetail,
  CaseFilters,
  CaseListResult,
  CaseRow,
  DisposePayload,
  DisposeResult,
} from "@/types/app";

/** 去掉空字符串 / undefined / 空数组，避免把 `status=` 这类空参数发给后端（后端会当成筛选条件）。 */
function clean(params: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== "")
  );
}

/** 案件分页列表（含各状态计数，供左栏筛选标签显示数字）。 */
export async function listCases(filters: Partial<CaseFilters>): Promise<CaseListResult> {
  const { data } = await api.get<ApiOk<CaseListResult>>("/cases", { params: clean({ ...filters }) });
  return data.data;
}

/** 案件详情（工作台中栏一次拿到全部素材）。 */
export async function getCaseDetail(caseNo: string): Promise<CaseDetail> {
  const { data } = await api.get<ApiOk<CaseDetail>>(`/cases/${caseNo}`);
  return data.data;
}

/** 接手案件：pending → processing。冲突（已被他人接手）时后端返回 40902。 */
export async function claimCase(caseNo: string): Promise<CaseRow> {
  const { data } = await api.post<ApiOk<CaseRow>>(`/cases/${caseNo}/claim`);
  return data.data;
}

/** 提交处置：双维度结论 + 风控动作联动。 */
export async function disposeCase(caseNo: string, payload: DisposePayload): Promise<DisposeResult> {
  const { data } = await api.post<ApiOk<DisposeResult>>(`/cases/${caseNo}/dispose`, payload);
  return data.data;
}

/** 归档单个案件（admin）：disposed → archived。 */
export async function archiveCase(
  caseNo: string
): Promise<{ result: ArchiveItemResult; case: CaseRow }> {
  const { data } = await api.post<ApiOk<{ result: ArchiveItemResult; case: CaseRow }>>(
    `/cases/${caseNo}/archive`
  );
  return data.data;
}

/** 批量归档（admin）：逐条返回成败，响应恒 200。 */
export async function archiveCases(caseNos: string[]): Promise<ArchiveResult> {
  const { data } = await api.post<ApiOk<ArchiveResult>>("/cases/archive", { case_nos: caseNos });
  return data.data;
}

/** 强制关闭（admin，原因必填 ≥3 字）。 */
export async function closeCase(caseNo: string, reason: string): Promise<CaseRow> {
  const { data } = await api.post<ApiOk<CaseRow>>(`/cases/${caseNo}/close`, { reason });
  return data.data;
}
