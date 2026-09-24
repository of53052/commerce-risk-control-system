/**
 * 与后端 schema 对齐的类型（app/schemas/*.py 的镜像）。
 *
 * 手工维护而非 codegen：P0 的 schema 面很小（认证 + 事件），
 * 引入 codegen 的成本高于收益；等接口到 P2 规模再评估。
 */

/** 后端统一响应封装（app/api/response.py）：code 是**字符串**，成功恒为 "0"。 */
export interface ApiOk<T> {
  code: string;
  message: string;
  data: T;
  trace_id: string;
}

/** 失败响应体：data 恒为 null，field/detail 用于表单级提示。 */
export interface ApiFailure {
  code: string;
  message: string;
  data: null;
  trace_id: string;
  field?: string | null;
  detail?: string | null;
}

/** app/schemas/auth.py::UserOut */
export interface CurrentUser {
  id: number;
  username: string;
  real_name: string | null;
  role: Role;
  status: string;
}

export type Role = "admin" | "strategist" | "auditor";

export type DecisionAction = "Pass" | "Challenge" | "Review" | "Reject";
export type RiskLevel = "low" | "mid" | "high";

/** app/main.py::healthz（依赖不健康时 HTTP 503，结构相同） */
export interface HealthZ {
  app: string;
  env: string;
  status: "ok" | "degraded";
  mysql: { status: string; version?: string; database?: string; detail?: string };
  redis: { status: string; version?: string; detail?: string };
}

/** 展示名：真实姓名优先，缺失时退回用户名（在 UI 层统一处理，避免各处 ?? 判断）。 */
export function displayName(user: CurrentUser | null): string {
  if (!user) return "未登录";
  return user.real_name || user.username;
}