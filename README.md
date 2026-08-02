# Personal Agent

Henson 的个人 Agent 项目：iOS App + 自托管 Agent Backend + Personal Data MCP。

PRD v1.0.1、Phase 1 技术方案 v0.1 和开发拆解 v0.1 均已通过评审；Henson 已于
2026-07-23 授权 Phase 1 开发。G0–G3 已通过，DEV-001–029 已实现至
`PROJECT_STATUS.md` 记录的当前证据边界，下一实现项是 CAP-001 Session Lifecycle
& Context Compaction，之后才进入 DEV-030 / G4。一期仍只做 Finance 单用户闭环；
iOS 是薄客户端，服务端负责 Agent Runtime、MCP Client、Policy、审计和定时任务。
Phase 1 Agent 框架为 Google ADK，业务模型通过可替换 adapter 接入。

## 当前进度

- Finance MCP、Governed MCP Client、Agent API/ADK composition、daily review、
  device identity、operator CLI 和 iOS SwiftPM 基础均已实现；
- 合成测试 Base 已完成真实 GLM → policy → MCP → Feishu 的端到端写入与重复拦截；
- iOS Simulator 已完成 Secure Enclave 注册、短期 token、capabilities 和撤销实测；
- APNs：2026-08-01 起已有付费 Apple Developer Program（落在原 team `4KKHF67AF3`，profile 有效期 7 天 → 1 年）；但 target 上的 Push Notifications capability 和服务端 `.p8` 尚未就绪，push sender 在此之前保持 fail-closed；
- CAP-001 的 Timeline/Session、长话题自动压缩和有界 Context 技术基线已冻结，
  但代码、迁移和 eval 尚未开始；
- CAP-002–008 记录后续 streaming、voice、memory、routing 和 multimodal 路线，
  不扩大当前 Finance Phase；
- Development Agent Loop 的 PRD、技术方案和 DAL-001–050 计划已落盘，
  其架构是 graph-orchestrated loop；但 Phase 1 Completion 前置尚未满足、
  DAL-G0 未授权，尚无任何 preflight 或自动化实现；
- 精确测试数、live evidence、已知限制和下一步以
  [PROJECT_STATUS.md](./PROJECT_STATUS.md) 为准。

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
- [Agent 横向能力 PRD v1.0](./docs/Agent横向能力PRD_v1.0.md)
- [Agent 横向能力技术方案 v1.0](./docs/Agent横向能力技术方案_v1.0.md)
- [开发 Agent 闭环 PRD v1.0](./docs/开发Agent闭环PRD_v1.0.md)
- [开发 Agent 闭环技术方案 v1.0](./docs/开发Agent闭环技术方案_v1.0.md)
- [开发 Agent 闭环开发拆解 v0.1](./docs/开发Agent闭环开发拆解_v0.1.md)
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

`personal-agent-eval-lint` 只验证样本来源与冻结合同，并报告样本分布；它不把样本数量
冒充模型分数。真实模型评测使用生产同款 Context Builder + ADK/GLM 边界，但仍不会
执行任何业务工具：

```bash
set -a
. ./.env.local
set +a
uv run personal-agent-model-eval --out /tmp/personal-agent-eval.jsonl
uv run personal-agent-eval-score --results /tmp/personal-agent-eval.jsonl
```

结果按精确来源标签 × Finance/授权/MCP 分列；每条结果绑定来源标签、输入、上下文与
expected output 的完整语义摘要，旧结果不能套到修改后的样本上，也不能与另一 evaluator
的结果混合后冒充一次评测。

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

- `evals/finance_v0.2.jsonl` 是按冻结合同维护的 69 条基线：17 条经 Henson 复核的
  `user_provided_redacted`、51 条 synthetic、1 条 PRD example。每条都用生成的工具
  schema 校验；缺个人/家庭归属一律追问、外币由服务端换算、多笔在 batch 关闭时一笔也不写。
- `evals/finance_expense_v0.1.jsonl` 保留为 Spike 期历史证据。它的期望包含已被推翻的默认个人
  归属、外币追问和两次单笔写，不得用于 Phase 1 验收。
- `user_provided_redacted` 不仅钉死 id 和输入，还用完整 SHA-256 绑定 reference time、
  prior turns 和 expected output；修改语义必须重新复核并显式更新 witness。
- 离线 eval 验证的是测试集合同和确定性 policy，不是模型准确率。
- 当前 Claude 证据是 smoke，不是完整准确率、时延或成本基准。
- ADK v0.2 是按用户决定在 23/30 时停止的部分运行；结果文件的 `case_count` 反映实际完成数量。
- 10–20 条经用户复核的脱敏真实表达属于 Phase 1 技术验证，不是 PRD 的前置条件。

## 下一步

下一步按以下顺序进行：

1. 完成 DEV-038 的 ECS 在线重启/网络矩阵及技术方案 §13.2 在线闭合。
2. 进入 DEV-039：write kill switch、restore 与服务/Nginx 回滚演练。
3. 继续遵守 G5：在单独授权前不接入生产 Finance 凭据或个人年度账本。
4. Development Agent Loop 保持独立、非阻塞；Phase 1 满足全部开发完成定义后，
   才可开始 DAL-001–006，随后由 Henson 单独通过 DAL-G0，再实现 DAL-007 起的
   Workflow Service 和只读 planning 切片。

## 安全约定

本仓库不保存任何个人健康原始数据、账单导出、私钥、证书、Token、`.env` 文件、服务器备份或生产数据库。所有示例凭证均使用占位符。
