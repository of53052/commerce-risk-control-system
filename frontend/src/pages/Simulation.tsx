import { Card, Typography } from "antd";

export default function SimulationPage() {
  return (
    <Card>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        事件仿真
      </Typography.Title>
      <Typography.Text type="secondary">P2 交付：模板注入 → 决策链路五步回溯。</Typography.Text>
    </Card>
  );
}