import { Card, Typography } from "antd";

export default function PolicyPage() {
  return (
    <Card>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        策略配置
      </Typography.Title>
      <Typography.Text type="secondary">P2 交付：规则树 / 名单 / 模型版本管理。</Typography.Text>
    </Card>
  );
}