# 测试分层

分层对应 Phase 1 技术方案 11.1（历史材料未随公开快照提供） 的测试层次，由
`DEV-001` 建立目录，由后续任务填充。

| 目录 | 放什么 |
|---|---|
| `unit/` | Decimal、日期、分类、Income Policy、旅行/退款匹配、家庭基金等确定性单元 |
| `contract/` | IR → JSON Schema、Host Context 不可伪造、错误 envelope、包边界 |
| `integration/` | MCP lifecycle、设备认证、连接器 fixture、飞书测试表 |
| `chaos/` | MCP 重启、网络断开、响应丢失、磁盘满、APNs/FX/飞书不可用 |
| `restore/` | AEAD 篡改与 AAD、key rotation、异机 restore、deletion manifest |

根目录下的 `test_dataset.py`、`test_policy.py`、`test_mcp_probe.py`、
`test_framework_probe.py` 和 `test_online_scoring.py` 属于 `personal_agent_spike`
的历史 Spike 证据，保持原样，不迁入上述分层，也不作为生产合同的验收。
其中 `test_dataset.py` 依赖未公开的旧版 Finance 数据集；公开快照不分发该数据集，
这 3 项测试会明确跳过，公开合成集由 `contract/test_evalset.py` 验证。

证据边界不得混淆：offline fixture、飞书测试表和生产灰度证据分开保存，任何一层通过
都不代表另一层"已验证"。
