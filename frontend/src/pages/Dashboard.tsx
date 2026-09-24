import { Card, Col, Row, Statistic, Typography } from "antd";

export default function DashboardPage() {
  return (
    <div>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        态势大盘
      </Typography.Title>
      <Row gutter={[16, 16]}>
        <Col span={6}><Card><Statistic title="今日事件" value={0} /></Card></Col>
        <Col span={6}><Card><Statistic title="拦截率" value={0} suffix="%" /></Card></Col>
        <Col span={6}><Card><Statistic title="待审案件" value={0} /></Card></Col>
        <Col span={6}><Card><Statistic title="命中规则" value={0} /></Card></Col>
      </Row>
    </div>
  );
}