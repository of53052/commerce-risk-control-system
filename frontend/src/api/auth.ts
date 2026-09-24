import { api } from "./client";
import type { ApiOk, CurrentUser } from "@/types/app";

export interface LoginResult {
  access_token: string;
  token_type: string;
  expires_at: string;
  user: CurrentUser;
}

export async function login(username: string, password: string): Promise<LoginResult> {
  const { data } = await api.post<ApiOk<LoginResult>>("/auth/login", { username, password });
  return data.data;
}

export async function me(): Promise<CurrentUser> {
  const { data } = await api.get<ApiOk<CurrentUser>>("/auth/me");
  return data.data;
}