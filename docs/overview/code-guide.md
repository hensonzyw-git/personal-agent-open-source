# 代码导读、参与方式与 FAQ


> 本版包含截至 2026-09-22 的已合入实现。历史线上证据与本次公开快照验证分开记录，完整手机开发交付链路尚未在本轮验收。见[验证说明](../verification.md)。
[项目首页](../../README.md) · [文档目录](./README.md)

按问题寻找源码入口，理解为什么没有 Quick Start，以及如何反馈和参与。

> 实现清单更新于 2026-09-23。文中的 2026-09-17 部署与实测记录属于历史证据；本轮离线结果见验证说明。

本篇导航：[代码导读](#code-map) · [为什么没有 Quick Start](#quick-start) · [常见问题](#faq) · [参与、反馈与许可](#contributing)

<a id="code-map"></a>
## 1. 代码导读

### 1.1 目录结构

以下为结构摘要，不是完整文件清单：

```text
.
├── ios/                       # 原生客户端与客户端测试
├── src/
│   ├── personal_agent/        # API、身份、运行时、上下文、工具治理
│   ├── personal_agent_core/   # 共享合同、工具 IR、基础设施与校验
│   ├── personal_data_mcp/     # 业务工具、连接器与业务域存储
│   ├── personal_agent_dal/    # 自动化开发链路
│   │   ├── machine/           # 状态机、绑定、授权、审查与恢复合同
│   │   ├── service/           # 控制面服务与操作入口
│   │   ├── worker/            # 工作区、执行、Supervisor 与验证
│   │   ├── github/            # GitHub 操作与外部副作用
│   │   ├── storage/           # 持久对象与迁移
│   │   └── frozen_contracts/  # 随包分发的冻结协议
│   ├── personal_agent_spike/  # 历史技术验证；不是生产模板
│   └── risk_monitor/          # 独立辅助模块；不作为主链路完成度依据
├── docs/
│   ├── overview/              # 本套公开导读：架构、流程、授权、恢复等
│   └── ...                    # 原始设计材料：逐份脱敏后决定是否保留
├── tests/                     # 合同、单元、集成及失败路径测试
├── evals/                     # 带来源和语义绑定的评测样本
├── spec/                      # 规范与配置材料
├── scripts/                   # 工程辅助脚本；执行前需审阅
├── deploy/                    # 原架构部署参考；不是通用安装包
├── AGENTS.md                  # 项目工程与 Agent 协作规则
├── PROJECT_STATUS.md          # 带历史阶段的状态记录
├── pyproject.toml
└── uv.lock
```

### 1.2 按问题阅读

| 你关心的问题 | 建议入口 |
| --- | --- |
| 模型如何变成受控业务调用？ | [runtime/host.py](../../src/personal_agent/runtime/host.py) → [policy](../../src/personal_agent/policy/) → [mcp_client](../../src/personal_agent/mcp_client/) → [personal_data_mcp](../../src/personal_data_mcp/) |
| DAL 为什么不能让模型直接改状态？ | [machine/engine.py](../../src/personal_agent_dal/machine/engine.py)、[machine/registry.py](../../src/personal_agent_dal/machine/registry.py) |
| 状态、产物与审批怎样绑定？ | [machine/binding.py](../../src/personal_agent_dal/machine/binding.py)、[storage/models.py](../../src/personal_agent_dal/storage/models.py) |
| 怎样避免一次动作被重复派发？ | [machine/action_lifecycle.py](../../src/personal_agent_dal/machine/action_lifecycle.py) |
| 断线与结果未知时怎么处理？ | [machine/action_recovery.py](../../src/personal_agent_dal/machine/action_recovery.py)、[machine/resume_authority.py](../../src/personal_agent_dal/machine/resume_authority.py) |
| ECS 与 Mac mini 如何分工？ | [部署拓扑](./architecture.md#architecture)、ECS / Home Mac ADR（原仓库路径：`docs/dal/ADR-ECS-orchestrator-home-mac-worker.md`）、[DAL 入口](../../deploy/nginx/dal-upstream.conf) |
| Worker 如何受限执行？ | [worker/poll_once.py](../../src/personal_agent_dal/worker/poll_once.py)、[worker/supervisor.py](../../src/personal_agent_dal/worker/supervisor.py) |
| Review 为什么需要新上下文和循环预算？ | [machine/review_fix_loop.py](../../src/personal_agent_dal/machine/review_fix_loop.py)、[machine/open_finding_set.py](../../src/personal_agent_dal/machine/open_finding_set.py) |
| 完整流程为什么不能只看模块数量？ | 完整流程优先规划（原仓库路径：`docs/dal/DAL_完整开发流程优先规划_v0.1.md`）、P0-02 审阅包（原仓库路径：`docs/dal/P0-02_统一审阅包_v0.1.md`） |

文档保留设计演进过程。阅读历史方案时，先看其日期、状态和后续覆盖说明，再检查当前代码；既不要拿旧草案覆盖新实现，也不要因为文件存在就推断目标已全部实现。

<a id="quick-start"></a>
## 2. 为什么没有 Quick Start

本次发布的目的不是让读者复刻作者的生产环境。公开代码与原系统之间还存在资源配置、设备注册、外部数据结构、凭据、签名、模型登录、Worker 准入和特定部署约束等差异。

因此，本文不提供一段会让人误以为“几条命令即可部署成功”的安装步骤。

阅读者仍然可以研究纯函数、合同、数据库事务、状态绑定及测试；进一步改造时，应自行构建隔离环境和合成数据，核对保留的源码、依赖与测试前提，关闭真实外部写入，再逐项接入自己的资源。编译成功或某些离线测试通过，不改变本仓库的发布定位。

<a id="faq"></a>
## 3. 常见问题

**这是一个可以直接安装到手机上的 App 吗？**

不是公开 App 发行版。iOS 代码是整个系统的一部分，依赖相应服务端、设备身份及业务配置。

**所有逻辑都在手机上运行吗？**

不是。ECS 负责日常 Agent 运行、策略和持久状态，同时承载 DAL 全局控制面；Mac mini Worker 执行受控研发任务。Calendar 的设计则因为事实源和平台权限而使用手机端执行与同步，不能把所有域强行放在同一位置。

**是否必须使用作者当前选择的模型？**

架构不以某个模型名称作为业务规则。但换模型或运行工具仍需要相应适配、合同校验与实际能力验证，不是替换字符串就能保证兼容。

**可以让 DAL 直接修改自己的权限、合并和部署吗？**

不能把“自我演进”解释成这种授权。需求、实现、交付、合并和部署分开处理；改变控制面、安全边界或高权限行为需要独立审阅。

**既然不能直接部署，开放代码有什么价值？**

价值在于可以看到真实个人 Agent 项目如何处理模型之外的工程问题：业务事实、幂等、版本化审批、状态迁移、执行所有权、独立审查、外部副作用和恢复。公开的是这些实现与取舍，不是一个包装成完成品的演示。

<a id="contributing"></a>
## 4. 参与、反馈与许可

本项目更适合围绕架构问题、协议缺口、可复现故障、文档错误和最小范围修复展开讨论。反馈请说明对应版本、预期与实际行为，以及可使用合成数据复现的步骤。

涉及新功能、权限或工作流变化时，先讨论需求和设计，再修改实现。提交中不要包含个人数据、真实凭据、生产日志、私有资源地址或他人的未授权材料。

冻结合同、生成文件及评测 witness 不能随意单独修改；需要理解其生成关系并同步验证。公开文档中的“已实现”“已验证”也属于需要维护的接口，代码变化后应同步更新。

**许可：** 本项目采用 [MIT License](../../LICENSE)，第三方材料保留各自许可。


<a id="glossary"></a>
## 术语速查

| 术语 | 本项目中的含义 |
| --- | --- |
| 控制面 | 保存权威状态，作出授权和调度决定的服务层 |
| ECS | 原系统的云端在线中心，承载日常 Agent 服务与 DAL 全局控制面 |
| Worker | 原部署中的家庭 Mac mini 执行端，执行被批准步骤，不决定全局权限 |
| Gate | 进入下一步前必须满足的条件或人工决定 |
| Artifact | PRD、方案、代码候选、报告等可版本化产物 |
| CAS | 只在当前版本仍等于预期版本时更新 |
| Capability | 针对具体动作和范围的有限执行许可 |
| Lease | 有效期内的执行所有权或策略许可 |
| Epoch / Fence | 用于区分新旧授权和执行所有者、防止旧执行继续生效的代际标识 |
| Receipt | 一次处理、观察、拒绝或接纳的可核对记录，不自动代表业务成功 |
| Reconciliation | 当结果不确定时，依据真实外部事实核对，而不是猜测或盲目重试 |

**长期目标不是让 Agent 脱离个人控制，而是让个人拥有一套能够理解、修改、审查并持续演进的 Agent 系统。**
