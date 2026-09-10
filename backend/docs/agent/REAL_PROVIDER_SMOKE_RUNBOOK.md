# Real Provider Smoke Runbook

仅在人工授权的本地环境执行；本文不保存任何密钥。

1. 由用户自行设置 `QWEN_API_KEY` 和 `DEEPSEEK_API_KEY` 环境变量。
2. 设置 `LLM_PROVIDER=dual`。
3. 首轮设置 `EXTERNAL_REASONING_DATA_MODE=mock_only`，禁止发送真实企业数据。
4. 重启 Backend，使配置在进程启动时重新加载。
5. 先检查 `/api/v1/agent/status`：配置存在只能显示 `configured_unverified`，不能显示已验证健康。
6. Qwen 最多执行 6 个授权 Smoke，逐次核对 route、actual provider、失败码与结构化语义帧。
7. DeepSeek 最多执行 4 个 Mock 数据 Smoke，确认匿名 P1/P2 标签且不能覆盖 Backend Gate。
8. 遇到 429、认证失败、模型不存在或超时立即停止重复请求，保留脱敏审计结果。
9. 完成 ACT8 v2 人工回归，重点检查上下文、显式实体优先级和响应范围。
10. 最后运行一次 Full Golden 与一次 Full pytest；失败时先区分 Provider、Fixture、Evaluator 和业务回归。

边界：不发送 Session/Tenant/Company/User ID、内部商品 ID、外部商品 ID、SKU、API Key 或未经批准的真实企业事实。
