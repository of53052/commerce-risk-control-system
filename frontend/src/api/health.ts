/** 健康检查：登录页展示"依赖是否就绪"。不健康时后端返回 HTTP 503，结构相同。 */
import { api } from "./client";
import type { HealthZ } from "@/types/app";

export async function healthz(): Promise<HealthZ> {
  const { data } = await api.get<HealthZ>("/healthz");
  return data;
}