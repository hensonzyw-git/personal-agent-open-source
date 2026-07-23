# Personal Agent

Henson 的个人 Agent 项目：iOS App + 自托管 Agent Backend + Personal Data MCP。

当前处于 Phase 0。iOS App 为薄客户端；服务端负责 Agent Runtime、MCP Client、工具策略、审计和自动任务。首选模型为智谱 GLM，Agent 框架将在 Google ADK 与 Claude Agent SDK 的同一组技术 Spike 后确定。

## 当前进度

- 产品范围、数据边界、权限分级和实施路线已完成 PRD v0.4；
- ECS 第一阶段安全加固完成：非 root SSH、公钥登录、UFW、Fail2ban、自动安全更新、Show Trace 停用；
- ECS 系统盘已迁移为 KMS 加密盘，并已配置每日自动快照和迁移后手动基线快照；
- 公网接入方案确定为 HTTPS + 设备身份，不把 Tailscale 作为移动端必需依赖。
- MCP 工具 IR v0.1 和 30 条记账基线评测集已建立；
- 独立 MCP Client 已真实跑通 stdio initialize、tool discovery 和结构化工具调用；
- Google ADK 已完成无 Key 的 MCP discovery；Claude Agent SDK 已完成无 Key 的 MCP 配置与 Client 构造。

## 文档

- [个人 Agent iOS App PRD v0.4](./个人Agent_PRD_v0.4.md)
- [ECS 安全加固实施记录](./ECS安全加固实施记录_2026-07-23.md)
- [MCP 工具 IR v0.1](./docs/MCP工具IR_v0.1.md)
- [Agent 框架与 GLM Spike 计划 v0.1](./docs/Agent框架Spike计划_v0.1.md)

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

## Spike 证据边界

- `evals/finance_expense_v0.1.jsonl` 的 30 条数据均有来源标签，当前不含真实个人账单。
- 离线 eval 验证的是测试集合同和确定性 policy，不是模型准确率。
- Claude Agent SDK 在无 Key 阶段只验证到配置和 Client 构造；其 MCP discovery 与 GLM 兼容性必须在连接 Agent 进程后实测。
- 最终选型前仍需增加 10–20 条经用户复核的脱敏真实表达。

## 下一步

1. 使用新 Key 跑 ADK / Claude Agent SDK + GLM 的同一组 30 条 eval。
2. 补充 Streamable HTTP、超时、重连、pending action 和审计 trace。
3. 输出框架选型报告、Phase 1 技术方案与开发任务拆解。
4. 补充异机加密备份和真实恢复演练。

## 安全约定

本仓库不保存任何个人健康原始数据、账单导出、私钥、证书、Token、`.env` 文件、服务器备份或生产数据库。所有示例凭证均使用占位符。
