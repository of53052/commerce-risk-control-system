/**
 * 健康检查：登录页展示"依赖是否就绪"。
 *
 * 两个关键点：
 * 1. 必须走 `probe`（根路径）而不是 `api`（`/api/v1`）。后端把 `/healthz` 注册在根路径，
 *    打到 `/api/v1/healthz` 会 404，登录页就会恒显示"MySQL 不可用 / Redis 不可用" ——
 *    恰好把"环境问题"伪装成"账号问题"，与这段状态条的设计意图完全相反。
 * 2. 依赖不健康时后端返回 **HTTP 503 但响应体结构与 200 一致**（`app/main.py`）。
 *    axios 默认把 503 视为异常，这里要把 body 捞回来继续用：MySQL 挂了而 Redis 正常时，
 *    应显示"Redis 正常 / MySQL 不可用"，把排障方向指准，而不是两个都写"不可用"。
 */
import { probe } from "./client";
import type { HealthZ } from "@/types/app";

export async function healthz(): Promise<HealthZ> {
  try {
    const { data } = await probe.get<HealthZ>("/healthz");
    return data;
  } catch (error) {
    const body = (error as { response?: { data?: HealthZ } }).response?.data;
    // 只有拿到"结构对得上"的 body 才降级使用；网络不通 / 代理返回 HTML 时照旧抛错，
    // 让调用方走 isLoading=false 且无 data 的"不可用"分支。
    if (body && typeof body === "object" && "status" in body) return body;
    throw error;
  }
}
