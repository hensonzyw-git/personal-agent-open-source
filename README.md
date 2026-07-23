# Personal Agent

Henson 的个人 Agent 项目：iOS App + 自托管 Agent Backend + Personal Data MCP。

PRD v1.0.1、Phase 1 技术方案 v0.1 和开发拆解 v0.1 均已通过评审；Henson 已于 2026-07-23 授权开发。Gate G0 与 G1 已通过，DEV-001 至 DEV-014 完成，下一步为 DEV-015。一期只做 Finance 记账场景；iOS App 为薄客户端，服务端负责 Agent Runtime、MCP Client、允许工具集合、审计和每日复核。首选模型为智谱 GLM；Phase 1 暂定使用 Google ADK，Claude Agent SDK 保留为候选和回退。

## 当前进度

- 一期产品范围、数据边界、权限分级和实施路线已完成 PRD v1.0.1 并通过评审；
- Phase 1 技术方案 v0.1 已补齐跨服务恢复、设备鉴权 wire contract 和加密/key rotation，并通过最终技术评审；
- Phase 1 开发拆解 v0.1 已定义 40 个任务、关键路径、G0-G6 七个 Gate、外部输入和完成定义；
- Wave 0–2（DEV-001～014）已完成：工具合同生成与漂移检测、两份 SQLite 与执行状态机、
  AEAD key ring、设备认证与 Host Context 绑定、独立 MCP Client Core、connector 隔离与
  Governed Tool Bridge；331 项测试通过，全程零凭证、未接触飞书；
- 正式 Finance 将使用独立、最小权限的飞书自建应用；现有全权限机器人应用只允许在合成测试 Base 做早期连通验证；
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
- [新 session 启动提示](./docs/新session启动提示.md)
- [个人 Agent iOS App PRD v1.0.1](./个人Agent_PRD_v1.0.1.md)
- [PRD v1.0.1 评审结论](./docs/PRD评审结论_2026-07-23.md)
- [Phase 1 技术方案设计计划](./docs/Phase1技术方案设计计划_v0.1.md)
- [Phase 1 技术方案 v0.1](./docs/Phase1技术方案_v0.1.md)
- [Phase 1 技术方案评审结论](./docs/Phase1技术方案评审结论_2026-07-23.md)
- [Phase 1 开发拆解 v0.1](./docs/Phase1开发拆解_v0.1.md)
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
uv run personal-agent-generate-contracts --check
uv run personal-agent-eval-lint
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

- `evals/finance_v0.2.jsonl` 是按冻结合同重建的 48 条基线：每条都用生成的工具 schema 校验过，
  缺个人/家庭归属一律追问、外币由服务端换算、多笔在 batch 关闭时一笔也不写。
- `evals/finance_expense_v0.1.jsonl` 保留为 Spike 期历史证据。它的期望包含已被推翻的默认个人
  归属、外币追问和两次单笔写，不得用于 Phase 1 验收。
- 目前没有任何 `user_provided_redacted` 数据；该标签由 DEV-037 在 Henson 实际复核后才可使用。
- 离线 eval 验证的是测试集合同和确定性 policy，不是模型准确率。
- 当前 Claude 证据是 smoke，不是完整准确率、时延或成本基准。
- ADK v0.2 是按用户决定在 23/30 时停止的部分运行；结果文件的 `case_count` 反映实际完成数量。
- 10–20 条经用户复核的脱敏真实表达属于 Phase 1 技术验证，不是 PRD 的前置条件。

## 下一步

下一步按以下顺序进行：

1. 从 DEV-015 继续，按[Phase 1 开发拆解](./docs/Phase1开发拆解_v0.1.md)的依赖顺序推进，逐个通过 G2–G6。
2. G2 前确认专用飞书应用已导入[最小权限清单](./docs/飞书Finance应用权限_v0.1.md)，并已被加为**合成测试 Base** 的编辑协作者；个人年度账本在 G5 前不授权。
3. G5 前增加 10–20 条经用户复核的脱敏真实表达，并完成异机加密备份和真实恢复演练。

## 安全约定

本仓库不保存任何个人健康原始数据、账单导出、私钥、证书、Token、`.env` 文件、服务器备份或生产数据库。所有示例凭证均使用占位符。
