# Personal Agent

Henson 的个人 Agent 项目：iOS App + 自托管 Agent Backend + Personal Data MCP。

PRD v1.0.1 已通过产品评审，当前在等待明确的 Phase 1 技术设计 / 开发授权。一期只做 Finance 记账场景；iOS App 为薄客户端，服务端负责 Agent Runtime、MCP Client、允许工具集合、审计和每日复核。首选模型为智谱 GLM；Phase 1 暂定使用 Google ADK，Claude Agent SDK 保留为候选和回退。

## 当前进度

- 一期产品范围、数据边界、权限分级和实施路线已完成 PRD v1.0.1 并通过评审；
- ECS 第一阶段安全加固完成：非 root SSH、公钥登录、UFW、Fail2ban、自动安全更新、Show Trace 停用；
- ECS 系统盘已迁移为 KMS 加密盘，并已配置每日自动快照和迁移后手动基线快照；
- 公网接入方案确定为 HTTPS + 设备身份，不把 Tailscale 作为移动端必需依赖。
- MCP 工具 IR v0.1 和 30 条记账基线评测集已建立；
- 独立 MCP Client 已真实跑通 stdio initialize、tool discovery 和结构化工具调用；
- Google ADK 和 Claude Agent SDK 均已通过 GLM-5.2 + MCP 的真实工具调用；
- ADK v0.2 已完成 23/30 条合成 eval 后按用户决定停止：严格通过率 95.65%，安全率、关键参数正确率和外部回执率均为 100%；
- Claude Agent SDK 已完成单笔和双笔 MCP smoke，但没有跑完整测试集，不与 ADK 的 23 条结果做伪横评。

## 文档

- [当前状态与交接清单](./PROJECT_STATUS.md)
- [项目协作与开发约定](./AGENTS.md)（与 `CLAUDE.md` 相同）
- [个人 Agent iOS App PRD v1.0.1](./个人Agent_PRD_v1.0.1.md)
- [PRD v1.0.1 评审结论](./docs/PRD评审结论_2026-07-23.md)
- [Phase 1 技术方案设计计划](./docs/Phase1技术方案设计计划_v0.1.md)
- [ECS 安全加固实施记录](./ECS安全加固实施记录_2026-07-23.md)
- [MCP 工具 IR v0.1](./docs/MCP工具IR_v0.1.md)
- [Agent 框架与 GLM Spike 计划 v0.1](./docs/Agent框架Spike计划_v0.1.md)
- [Agent 框架与 GLM Spike 初步结果](./docs/Agent框架Spike初步结果_2026-07-23.md)

## 本地运行

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --all-extras --dev
uv run pytest -q
uv run personal-agent-mcp-probe
uv run personal-agent-framework-probe
uv run personal-agent-offline-eval
```

这些命令默认不会发起模型请求，也不会写入飞书。

如需开始真实 GLM Spike，请先在智谱控制台创建一个新的 Key，再使用交互脚本写入本地、被 Git 忽略且权限为 `600` 的 `.env.local`：

```bash
sh scripts/configure_local_key.sh
```

不要把 Key 粘贴到聊天、Git、命令行参数、测试快照或日志。

在线 eval 会真实消耗模型 API，但仍只调用本地 fixture：

```bash
set -a
. ./.env.local
set +a
uv run personal-agent-online-eval --framework adk --case-id FIN-021
uv run personal-agent-online-eval --framework claude --case-id FIN-021
```

## Spike 证据边界

- `evals/finance_expense_v0.1.jsonl` 的 30 条数据均有来源标签，当前不含真实个人账单。
- 离线 eval 验证的是测试集合同和确定性 policy，不是模型准确率。
- 当前 Claude 证据是 smoke，不是完整准确率、时延或成本基准。
- ADK v0.2 是按用户决定在 23/30 时停止的部分运行；结果文件的 `case_count` 反映实际完成数量。
- 10–20 条经用户复核的脱敏真实表达属于 Phase 1 技术验证，不是 PRD 的前置条件。

## 下一步

收到用户明确的 Phase 1 技术方案设计授权后，按以下顺序开始：

1. 按 [Phase 1 技术方案设计计划](./docs/Phase1技术方案设计计划_v0.1.md) 输出技术方案、数据模型和开发任务拆解；此时不开始实现。
2. 以 ADK 为主框架验证 Streamable HTTP、超时、重连、服务端允许工具集合和审计 trace。
3. 增加 10–20 条经用户复核的脱敏真实表达；只有 ADK 触发选型门时，才为 Claude 跑同批完整回归。
4. 补充异机加密备份和真实恢复演练；技术方案评审通过并获得开发授权后，才进入实施。

## 安全约定

本仓库不保存任何个人健康原始数据、账单导出、私钥、证书、Token、`.env` 文件、服务器备份或生产数据库。所有示例凭证均使用占位符。
