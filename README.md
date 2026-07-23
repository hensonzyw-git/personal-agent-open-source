# Personal Agent

Henson 的个人 Agent 项目：iOS App + 自托管 Agent Backend + Personal Data MCP。

当前仍处于架构与实现准备阶段。iOS App 为薄客户端；服务端负责 Agent Runtime、MCP Client、工具策略、审计和自动任务。首选模型为智谱 GLM，Agent 框架将在 Google ADK 与 Claude Agent SDK 的同一组技术 Spike 后确定。

## 当前进度

- 产品范围、数据边界、权限分级和实施路线已完成 PRD v0.4；
- ECS 第一阶段安全加固完成：非 root SSH、公钥登录、UFW、Fail2ban、自动安全更新、Show Trace 停用；
- ECS 系统盘已迁移为 KMS 加密盘，并已配置每日自动快照和迁移后手动基线快照；
- 公网接入方案确定为 HTTPS + 设备身份，不把 Tailscale 作为移动端必需依赖。

## 文档

- [个人 Agent iOS App PRD v0.4](./个人Agent_PRD_v0.4.md)
- [ECS 安全加固实施记录](./ECS安全加固实施记录_2026-07-23.md)

## 下一步

1. 补充异机加密备份和真实恢复演练。
2. 定义 MCP 工具 IR，并完成 GLM + Agent SDK 技术 Spike。
3. 实现首个垂直链路：iPhone 自然语言记账 → MCP → 飞书真实写入 → 审计与每日人工复核。

## 安全约定

本仓库不保存任何个人健康原始数据、账单导出、私钥、证书、Token、`.env` 文件、服务器备份或生产数据库。所有示例凭证均使用占位符。
