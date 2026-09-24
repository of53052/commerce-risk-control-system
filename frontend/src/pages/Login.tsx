/**
 * 登录页（DESIGN.md §2.5 / §9）：品牌感的第二个落点。
 *
 * 设计取舍：
 * * 背景用极浅的品牌渐变（`#F8FAFC → #EFF4FF`），**不做**满屏深色或大图 —
 *   这是"企业级浅色"与"营销落地页"的分界，风控系统要让人看见真实状态；
 * * 唯一的强视觉是 Logo 的 45° 渐变方块（`#1E5EFF → #7C3AED`）；
 * * 卡片下方常驻"依赖状态"，把后端 /healthz 的真实结果摆在登录入口 —
 *   演示时一眼就能看出"是风控挂了还是我密码错了"，避免把环境问题误判为账号问题。
 */
import { Button, Card, Form, Input, Space, Tag, Typography, message } from "antd";
import { LockOutlined, SafetyCertificateOutlined, UserOutlined } from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { login, me } from "@/api/auth";
import { healthz } from "@/api/health";
import { useAuthStore } from "@/store/auth";
import { LOGIN_TOKENS } from "@/theme/brand";

const ACCOUNTS = [
  { role: "管理员", username: "admin", password: "admin123" },
  { role: "策略运营", username: "strategist", password: "strategy123" },
  { role: "风控审核员", username: "auditor", password: "audit123" },
];

export default function LoginPage() {
  const navigate = useNavigate();
  const { setSession } = useAuthStore();

  const health = useQuery({ queryKey: ["healthz"], queryFn: healthz, retry: false, refetchInterval: 30_000 });

  const doLogin = useMutation({
    mutationFn: ({ username, password }: { username: string; password: string }) => login(username, password),
    onSuccess: async (result) => {
      // 先用登录返回的用户信息落一个会话，再拉 /auth/me 校验令牌可用
      // （两步都成功才进主框架：令牌签发成功但立刻失效时，用户会卡在登录页反复点）
      setSession(result.access_token, result.user);
      try {
        const user = await me();
        setSession(result.access_token, user);
        navigate("/dashboard", { replace: true });
      } catch {
        useAuthStore.getState().clear();
        message.error("登录状态校验失败，请重试");
      }
    },
    onError: (error: unknown) => {
      const err = error as { response?: { data?: { message?: string; trace_id?: string } } };
      const text = err.response?.data?.message ?? "登录失败，请检查网络或后端服务";
      const trace = err.response?.data?.trace_id;
      message.error(trace ? `${text}（trace_id: ${trace}）` : text);
    },
  });

  const mysqlOk = health.data?.mysql?.status === "ok";
  const redisOk = health.data?.redis?.status === "ok";

  return (
    <div
      style={{
        minHeight: "100vh",
        background: LOGIN_TOKENS.bg,
        display: "grid",
        placeItems: "center",
        padding: 24,
      }}
    >
      <div className="rise" style={{ width: "min(420px, 100%)" }}>
        <Space align="center" size={12} style={{ marginBottom: 20 }}>
          <div
            style={{
              width: 40,
              height: 40,
              borderRadius: 11,
              background: LOGIN_TOKENS.logoGradient,
              display: "grid",
              placeItems: "center",
              flex: "0 0 auto",
            }}
          >
            <SafetyCertificateOutlined style={{ color: "#fff", fontSize: 21 }} />
          </div>
          <div>
            <Typography.Title level={3} style={{ margin: 0, letterSpacing: -0.2 }}>
              风控中台
            </Typography.Title>
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              规则与模型协同的实时决策链路
            </Typography.Text>
          </div>
        </Space>

        <Card
          variant="borderless"
          style={{ boxShadow: LOGIN_TOKENS.cardShadow, borderRadius: LOGIN_TOKENS.cardRadius }}
          styles={{ body: { padding: 28 } }}
        >
          <Form
            layout="vertical"
            requiredMark={false}
            initialValues={{ username: "admin", password: "admin123" }}
            onFinish={(values) => doLogin.mutate(values)}
          >
            <Form.Item name="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}>
              <Input prefix={<UserOutlined />} size="large" autoComplete="username" />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}>
              <Input.Password
                prefix={<LockOutlined />}
                size="large"
                autoComplete="current-password"
                onPressEnter={() => undefined}
              />
            </Form.Item>
            <Button type="primary" htmlType="submit" size="large" block loading={doLogin.isPending}>
              登录
            </Button>
          </Form>

          <div style={{ marginTop: 20, paddingTop: 16, borderTop: "1px solid #F1F5F9" }}>
            <Space size={6} wrap>
              <Tag color={health.isLoading ? "default" : mysqlOk ? "success" : "error"} style={{ marginInlineEnd: 0 }}>
                MySQL {health.isLoading ? "检查中" : mysqlOk ? health.data?.mysql.version ?? "ok" : "不可用"}
              </Tag>
              <Tag color={health.isLoading ? "default" : redisOk ? "success" : "error"} style={{ marginInlineEnd: 0 }}>
                Redis {health.isLoading ? "检查中" : redisOk ? health.data?.redis.version ?? "ok" : "不可用"}
              </Tag>
            </Space>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: "10px 0 0" }}>
              演示账号：
              {ACCOUNTS.map((item, index) => (
                <span key={item.username}>
                  {index > 0 ? "、" : ""}
                  {item.role} {item.username}
                </span>
              ))}
            </Typography.Paragraph>
          </div>
        </Card>
      </div>
    </div>
  );
}