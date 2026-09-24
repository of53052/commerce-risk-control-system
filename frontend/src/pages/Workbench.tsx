import { Card, Typography } from "antd";

export default function WorkbenchPage() {
  return (
    <Card>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        审核工作台
      </Typography.Title>
      <Typography.Text type="secondary">P1 交付：案件列表 → 详情 → 处置闭环。</Typography.Text>
    </Card>
  );
}