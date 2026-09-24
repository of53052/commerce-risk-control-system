/**
 * 主框架布局（DESIGN.md §2.5 / §4 / §12）：深色侧边导航 + 浅色顶栏 + 内容区。
 *
 * 品牌感的落点只有两处（DESIGN.md §9）：Logo 渐变与选中指示条。
 * 色彩一律取自 theme/brand.ts，不在此处写死色值。
 */
import { Suspense } from "react";
import { Layout, Menu, Spin, Typography, Avatar, Space, Dropdown, Tag } from "antd";
import {
  DashboardOutlined,
  AuditOutlined,
  FileSearchOutlined,
  ExperimentOutlined,
  LogoutOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuthStore } from "@/store/auth";
import { BRAND_GRADIENT, NAV_TOKENS } from "@/theme/brand";
import { LAYOUT } from "@/theme/layout";
import { displayName, type Role } from "@/types/app";

const { Sider, Header, Content } = Layout;

/** 角色 → 可见菜单（PRD §7 权限矩阵：前端只做显隐，真正的边界在后端）。 */
const MENUS: Record<Role, { key: string; icon: JSX.Element; label: string }[]> = {
  admin: [
    { key: "/dashboard", icon: <DashboardOutlined />, label: "态势大盘" },
    { key: "/workbench", icon: <FileSearchOutlined />, label: "审核工作台" },
    { key: "/policy", icon: <AuditOutlined />, label: "策略配置" },
    { key: "/simulation", icon: <ExperimentOutlined />, label: "事件仿真" },
  ],
  strategist: [
    { key: "/dashboard", icon: <DashboardOutlined />, label: "态势大盘" },
    { key: "/policy", icon: <AuditOutlined />, label: "策略配置" },
    { key: "/simulation", icon: <ExperimentOutlined />, label: "事件仿真" },
  ],
  auditor: [
    { key: "/dashboard", icon: <DashboardOutlined />, label: "态势大盘" },
    { key: "/workbench", icon: <FileSearchOutlined />, label: "审核工作台" },
  ],
};

const ROLE_LABEL: Record<Role, string> = {
  admin: "管理员",
  strategist: "策略运营",
  auditor: "风控审核员",
};

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, clear } = useAuthStore();

  const role: Role = user?.role ?? "auditor";
  const items = MENUS[role];
  const activeKey = items.find((item) => location.pathname.startsWith(item.key))?.key;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider width={LAYOUT.siderWidth} collapsedWidth={LAYOUT.siderCollapsedWidth} style={{ background: NAV_TOKENS.bg }}>
        {/* Logo：全站唯一使用品牌渐变的地方之一（DESIGN.md §9） */}
        <div style={{ height: LAYOUT.headerHeight, display: "flex", alignItems: "center", gap: 10, padding: "0 20px" }}>
          <div
            style={{
              width: 26,
              height: 26,
              borderRadius: 7,
              background: `linear-gradient(135deg, ${BRAND_GRADIENT.from}, ${BRAND_GRADIENT.to})`,
              flex: "0 0 auto",
            }}
          />
          <Typography.Text style={{ color: "#fff", fontWeight: 600, fontSize: 15, whiteSpace: "nowrap" }}>
            风控中台
          </Typography.Text>
        </div>

        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={activeKey ? [activeKey] : []}
          items={items}
          onClick={({ key }) => navigate(key)}
          style={{ background: "transparent", borderInlineEnd: "none", marginTop: 8 }}
          data-testid="nav-menu"
        />
      </Sider>

      <Layout>
        <Header
          style={{
            height: LAYOUT.headerHeight,
            lineHeight: `${LAYOUT.headerHeight}px`,
            background: "#fff",
            borderBottom: "1px solid #F1F5F9",
            padding: "0 24px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <Typography.Text strong style={{ fontSize: 15 }}>
            {items.find((item) => item.key === activeKey)?.label ?? "风控中台"}
          </Typography.Text>

          <Space size={16}>
            <Tag color="default" style={{ marginInlineEnd: 0 }}>
              {ROLE_LABEL[role]}
            </Tag>
            <Dropdown
              menu={{
                items: [
                  {
                    key: "logout",
                    icon: <LogoutOutlined />,
                    label: "退出登录",
                    onClick: () => {
                      clear();
                      navigate("/login", { replace: true });
                    },
                  },
                ],
              }}
            >
              <Space style={{ cursor: "pointer" }}>
                <Avatar size={28} style={{ background: "#1E5EFF" }} icon={<UserOutlined />} />
                <Typography.Text style={{ color: NAV_TOKENS.bg }}>{displayName(user)}</Typography.Text>
              </Space>
            </Dropdown>
          </Space>
        </Header>

        <Content style={{ padding: LAYOUT.contentPadding }}>
          <Suspense fallback={<Spin style={{ display: "block", margin: "80px auto" }} />}>
            <Outlet />
          </Suspense>
        </Content>
      </Layout>
    </Layout>
  );
}
