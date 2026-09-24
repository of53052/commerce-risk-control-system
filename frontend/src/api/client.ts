import axios, { AxiosHeaders } from "axios";
import { useAuthStore } from "@/store/auth";

export const api = axios.create({
  baseURL: "/api/v1",
  timeout: 15_000,
});

/**
 * 探针客户端：用**根路径**请求，不加 `/api/v1` 前缀。
 *
 * 为什么单独开一个实例：健康检查 `/healthz` 是给启动脚本、验证脚本、监控用的基础设施端点，
 * 它在后端注册在根路径上（`app/main.py`），刻意不随 API 版本演进 —— 探针不该因为 API 升到 v2
 * 就跟着改地址。用 `api` 实例请求它会打到 `/api/v1/healthz`（后端无此路由，恒 404）。
 */
export const probe = axios.create({
  baseURL: "/",
  timeout: 5_000,
});

api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) {
    config.headers = AxiosHeaders.from(config.headers);
    config.headers.set("Authorization", `Bearer ${token}`);
  }
  return config;
});

api.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error.response?.status === 401) {
      useAuthStore.getState().clear();
    }
    return Promise.reject(error);
  }
);

/**
 * 从 axios 错误里取出后端统一响应体的中文提示，并附上 `trace_id`。
 *
 * 为什么要带 trace_id：风控场景里"这个 409 到底是并发冲突还是状态不对"，
 * 靠界面上的一句话是判断不了的；带上 trace_id 才能在后端日志里直接定位到那一次请求。
 * 后端响应结构见 `app/api/response.py`（code 为字符串，成功恒为 "0"）。
 */
export function apiErrorMessage(error: unknown, fallback: string): string {
  // 必须先判空：TanStack Query 在"请求还没失败"时 error 就是 null，
  // 而调用方常在渲染期无条件调用本函数（如 `apiErrorMessage(query.error, ...)`）。
  // 在渲染期抛异常会让整个 React 树卸载 —— 表现为整页白屏，很难反查到这一行。
  if (error === null || error === undefined) return fallback;
  const err = error as { response?: { data?: { message?: string; trace_id?: string } } };
  const text = err.response?.data?.message ?? fallback;
  const trace = err.response?.data?.trace_id;
  return trace ? `${text}（trace_id: ${trace}）` : text;
}
