/**
 * 工作台右栏：研判与处置协同区（PRD §11.3 第三栏）。
 *
 * 这一栏要解决的唯一问题：**让审核员在看见证据后，用最小的认知负担做出可审计的处置**。
 * 因此三件事必须做对：
 * 1. 上方复述"系统怎么判的"（分数 / 等级 / 建议 / 主要依据），不与中栏重复展开；
 * 2. 每个选项都带**副作用说明**，尤其是会写名单库的动作（跨案件生效，不能事后才知道）；
 * 3. 提交前二次确认 + 提交后逐项展示联动结果（success / skipped / failed 三档含义不同）。
 *
 * 状态门禁与后端一致（PRD §9.2）：pending 不能直接处置，非当前处理人的 auditor 不能处置他人案件，
 * admin 是兜底例外。前端做门禁只是"不让明显非法的操作被点下去"，最终判定仍在服务端。
 */
import { App, Alert, Button, Card, Checkbox, Divider, Empty, Input, Radio, Space, Tag, Typography } from "antd";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { apiErrorMessage } from "@/api/client";
import { disposeCase } from "@/api/cases";
import { ActionTag, ExecResultTag, RiskTag } from "@/components/common/Tags";
import {
  BIZ_RESULT_META,
  DECIDED_BY_LABEL,
  RISK_ACTION_LABEL,
  RISK_ACTION_META,
  RISK_ACTION_ORDER,
} from "@/constants/caseMeta";
import { LAYOUT } from "@/theme/layout";
import { SURFACES } from "@/theme/tokens";
import { formatScore, formatSigned } from "@/utils/datetime";
import type { BizResult, CaseDetail, DisposeResult, RiskAction, Role } from "@/types/app";

/** 处置备注最小长度，与后端 `MIN_REMARK_LENGTH` 一致（PRD §9.3）。 */
const MIN_REMARK_LENGTH = 10;

interface Props {
  detail: CaseDetail | undefined;
  role: Role;
  currentUserName: string | null;
  /** 处置成功后通知上层失效案件列表与详情查询。 */
  onChanged: () => void;
}

/** 系统判定摘要：综合分 / 等级 / 动作 / 来源 + 两条最主要依据。 */
function SystemVerdict({ detail }: { detail: CaseDetail }) {
  const decision = detail.focus.decision;
  const topRule = detail.focus.hit_rules[0];
  const topContribution = detail.focus.model_contributions[0];
  return (
    <Card size="small" title="系统判定" styles={{ body: { paddingTop: 12 } }}>
      <Space size={24} align="start">
        <div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            综合分
          </Typography.Text>
          <div className="mono" style={{ fontSize: 30, lineHeight: 1.1 }}>
            {formatScore(decision?.risk_score ?? detail.case.max_score, 0)}
          </div>
        </div>
        <div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            风险等级 / 建议动作
          </Typography.Text>
          <div style={{ marginTop: 4 }}>
            <Space size={6}>
              <RiskTag level={detail.case.risk_level} showScore={false} />
              {decision ? <ActionTag action={decision.action} /> : null}
            </Space>
          </div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {decision ? DECIDED_BY_LABEL[decision.decided_by] ?? decision.decided_by : "-"}
          </Typography.Text>
        </div>
      </Space>

      <Divider style={{ margin: "12px 0" }} />

      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          证据提示
        </Typography.Text>
        {topRule ? (
          <div style={{ fontSize: 12 }}>
            最强命中：<span className="mono">{topRule.rule_code}</span>（+{topRule.score} 分）{topRule.rule_name}
          </div>
        ) : (
          <div style={{ fontSize: 12, color: "#64748B" }}>本次决策没有命中规则</div>
        )}
        {topContribution ? (
          <div style={{ fontSize: 12 }}>
            模型主导特征：<span className="mono">{topContribution.feature_name}</span>（
            <span className="mono">{formatSigned(topContribution.contribution)}</span>）
          </div>
        ) : (
          <div style={{ fontSize: 12, color: "#64748B" }}>模型未参与本次决策</div>
        )}
        <div style={{ fontSize: 12, color: "#64748B" }}>
          共命中 {detail.focus.hit_rules.length} 条规则，关联事件 {detail.case.event_cnt} 次
        </div>
      </Space>
    </Card>
  );
}

export default function DisposalPanel({ detail, role, currentUserName, onChanged }: Props) {
  const { message, modal } = App.useApp();
  const [businessResult, setBusinessResult] = useState<BizResult>("approve");
  const [actions, setActions] = useState<RiskAction[]>([]);
  const [remark, setRemark] = useState("");
  const [result, setResult] = useState<DisposeResult | null>(null);

  const caseNo = detail?.case.case_no ?? null;

  // 切换案件必须清空上一条的处置草稿：否则"在 A 案件填的内容被带到 B 案件提交"
  // 是风控里最危险的一类误操作（结论正确、对象错误）。
  useEffect(() => {
    setBusinessResult("approve");
    setActions([]);
    setRemark("");
    setResult(null);
  }, [caseNo]);

  const mutation = useMutation({
    mutationFn: (payload: { business_result: BizResult; risk_actions: RiskAction[]; remark: string }) =>
      disposeCase(caseNo as string, payload),
    onSuccess: (data) => {
      setResult(data);
      const failed = data.items.filter((item) => item.exec_result === "failed").length;
      const labels = data.risk_actions.map((key) => RISK_ACTION_META[key]?.label ?? key).join("、");
      if (failed > 0) {
        message.warning(`处置已提交，但有 ${failed} 项联动需要人工介入（见下方联动结果）`);
      } else {
        message.success(
          `处置已提交：${BIZ_RESULT_META[data.business_result].label}${data.risk_actions.length ? `，措施「${labels}」` : ""}`
        );
      }
      onChanged();
    },
    onError: (error: unknown) => message.error(apiErrorMessage(error, "处置提交失败")),
  });

  if (!detail) {
    return (
      <div
        style={{
          ...SURFACES.card,
          width: LAYOUT.workbench.rightWidth,
          flex: "0 0 auto",
          height: "100%",
          display: "grid",
          placeItems: "center",
        }}
      >
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选中案件后可在此提交处置" />
      </div>
    );
  }

  const row = detail.case;
  const mine = Boolean(currentUserName) && row.handler === currentUserName;

  // 状态与权限门禁（顺序与后端一致：先看状态，再看是不是当前处理人）
  let gate: string | null = null;
  if (role === "strategist") {
    gate = "策略运营角色只读案件，不参与处置";
  } else if (row.status === "pending") {
    gate = role === "admin" ? null : "案件尚未接手：请先接手，再提交处置";
  } else if (row.status === "processing") {
    gate = mine || role === "admin" ? null : "只有当前处理人才能处置该案件";
  } else {
    gate = `案件已${row.status === "disposed" ? "处置" : row.status === "archived" ? "归档" : "关闭"}，不能重复提交`;
  }

  const remarkReady = remark.trim().length >= MIN_REMARK_LENGTH;
  const actionReady = actions.length > 0 && (!actions.includes("pass") || actions.length === 1);
  const listEffects = actions
    .map((key) => RISK_ACTION_META[key]?.listEffect)
    .filter((item): item is string => Boolean(item));

  /** pass 与其他动作互斥（与后端 `EXCLUSIVE_RISK_ACTION` 规则一致）。 */
  function toggleAction(key: RiskAction, checked: boolean) {
    if (!checked) {
      setActions(actions.filter((item) => item !== key));
      return;
    }
    if (key === "pass") {
      setActions(["pass"]);
      return;
    }
    setActions([...actions.filter((item) => item !== "pass"), key]);
  }

  function submit() {
    modal.confirm({
      title: "确认提交处置？",
      width: 460,
      okText: "确认执行",
      cancelText: "取消",
      okButtonProps: { danger: businessResult === "reject" },
      content: (
        <div style={{ fontSize: 13 }}>
          <div>
            业务结论：<b>{BIZ_RESULT_META[businessResult].label}</b>（{BIZ_RESULT_META[businessResult].effect}）
          </div>
          <div style={{ marginTop: 6 }}>
            风控动作：
            <b>{actions.map((key) => RISK_ACTION_META[key].label).join("、")}</b>
          </div>
          <div style={{ marginTop: 6, color: "#64748B" }}>
            该操作会改动真实业务单据 / 名单库，并记入审计日志（哈希链），不可撤销。
          </div>
        </div>
      ),
      onOk: () =>
        mutation
          .mutateAsync({ business_result: businessResult, risk_actions: actions, remark: remark.trim() })
          .then(() => undefined),
    });
  }

  return (
    <div
      style={{
        ...SURFACES.card,
        width: LAYOUT.workbench.rightWidth,
        flex: "0 0 auto",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        height: "100%",
      }}
    >
      <div style={{ padding: "12px 16px", borderBottom: "1px solid #F1F5F9" }}>
        <Typography.Text strong>研判与处置</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
          {mine ? "我接手的案件" : row.handler ? `处理人 ${row.handler}` : "尚未接手"}
        </Typography.Text>
      </div>

      <div
        style={{
          flex: "1 1 auto",
          overflowY: "auto",
          padding: 16,
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <SystemVerdict detail={detail} />

        {result ? <LinkageResult result={result} /> : null}

        {gate ? (
          <Alert type="info" showIcon message={gate} />
        ) : null}

        <Card size="small" title="处置结论" styles={{ body: { paddingTop: 12 } }}>
          <Radio.Group
            value={businessResult}
            onChange={(event) => setBusinessResult(event.target.value as BizResult)}
            disabled={Boolean(gate)}
            style={{ width: "100%" }}
          >
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {(["approve", "reject"] as BizResult[]).map((key) => (
                <Radio key={key} value={key}>
                  <Space size={6}>
                    <Tag color={BIZ_RESULT_META[key].tag} style={{ marginInlineEnd: 0 }}>
                      {BIZ_RESULT_META[key].label}
                    </Tag>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {BIZ_RESULT_META[key].effect}
                    </Typography.Text>
                  </Space>
                </Radio>
              ))}
            </Space>
          </Radio.Group>
        </Card>

        <Card size="small" title="风控动作（可多选）" styles={{ body: { paddingTop: 12 } }}>
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            {RISK_ACTION_ORDER.map((key) => {
              const meta = RISK_ACTION_META[key];
              return (
                <Checkbox
                  key={key}
                  checked={actions.includes(key)}
                  disabled={Boolean(gate)}
                  onChange={(event) => toggleAction(key, event.target.checked)}
                  style={{ alignItems: "flex-start" }}
                >
                  <Space size={6} wrap>
                    <span>{meta.label}</span>
                    {key === "pass" ? (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        （与其他动作互斥）
                      </Typography.Text>
                    ) : null}
                  </Space>
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {meta.effect}
                    </Typography.Text>
                  </div>
                </Checkbox>
              );
            })}
          </Space>
        </Card>

        {listEffects.length > 0 ? (
          <Alert
            type="warning"
            showIcon
            message="本次处置将写入名单库（跨案件生效）"
            description={listEffects.join("、")}
          />
        ) : null}

        <Card size="small" title="处置原因备注" styles={{ body: { paddingTop: 12 } }}>
          <Input.TextArea
            rows={4}
            maxLength={500}
            showCount
            disabled={Boolean(gate)}
            value={remark}
            onChange={(event) => setRemark(event.target.value)}
            placeholder="写清判断依据，例如：1 小时内下单 5 次且同设备关联 3 个账号，判定为批量薅券"
          />
          <Typography.Text type={remarkReady ? "secondary" : "danger"} style={{ fontSize: 12 }}>
            至少 {MIN_REMARK_LENGTH} 个字（当前 {remark.trim().length} 字）
          </Typography.Text>
        </Card>

        <Button
          type="primary"
          block
          danger={businessResult === "reject"}
          size="large"
          loading={mutation.isPending}
          disabled={Boolean(gate) || !actionReady || !remarkReady}
          onClick={submit}
        >
          {role === "admin" && row.status === "pending" ? "以管理员身份直接处置" : "提交处置"}
        </Button>

        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          提交后联动结果会逐项展示；「需要人工介入」的项请记录后线下核对业务库。
        </Typography.Text>
      </div>
    </div>
  );
}

/** 联动结果逐项展示（提交后）。 */
function LinkageResult({ result }: { result: DisposeResult }) {
  return (
    <Card size="small" title="联动结果" styles={{ body: { paddingTop: 12 } }}>
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        {result.items.length === 0 ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            本次处置没有产生联动动作
          </Typography.Text>
        ) : (
          result.items.map((item, index) => {
            const detailText = item.detail?.reason ?? item.target_id ?? "";
            return (
              <Space key={`${item.risk_action}-${index}`} size={8} style={{ width: "100%", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12 }}>
                  {RISK_ACTION_LABEL[item.risk_action] ?? item.risk_action}
                  {detailText ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                      {String(detailText)}
                    </Typography.Text>
                  ) : null}
                </span>
                <ExecResultTag result={item.exec_result} />
              </Space>
            );
          })
        )}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          「无对象执行」是预期内的业务状态（如订单已支付、事件无设备号）；
          「需人工介入」表示数据不一致，需要人工核对业务库。
        </Typography.Text>
      </Space>
    </Card>
  );
}
