# 爆款拆解智谱主通道与 GPT 安全回退设计

## 背景与根因

克隆服务器运行时配置为 `BREAKDOWN_MODEL=glm-4v-plus`，但 `content_domains.breakdown._chat_multimodal` 无条件把请求发送到 OpenAI 的 `/v1/chat/completions`。OpenAI 不提供 `glm-4v-plus`，因此拆解在视频下载、抽帧和 ASR 均成功后返回 HTTP 404，并触发退点。

## 目标

- 爆款拆解和提示词反推默认使用智谱 `glm-4v-plus`。
- 智谱发生可证明“请求尚未送达”的网络错误时，自动回退到 OpenAI `gpt-4o`。
- 智谱可能已经收到请求时禁止自动回退，避免重复计费。
- 保持每个链接 20 点以及现有单条、批量失败退点规则不变。
- 仅部署到克隆服务器 `8.148.158.106`，不修改主服务器。

## 方案比较与选择

1. **智谱主通道 + 安全 GPT 回退（采用）**：成本较低；仅在 DNS、连接拒绝、主机/网络不可达或 TLS 握手失败等投递前错误时回退。
2. 智谱任意失败均回退 GPT：成功率更高，但超时、连接关闭或 HTTP 错误后再次请求可能造成双重计费。
3. 智谱失败后由用户确认 GPT 重试：成本透明，但需要前端状态与交互改造，超出本次最小修复范围。

## 请求路由

`_chat_multimodal` 继续统一构造 OpenAI 兼容的多模态消息体，但模型与鉴权按通道分别设置：

1. 主通道读取 `REVERSE_ZHIPU_KEY`，模型读取 `BREAKDOWN_MODEL`，默认 `glm-4v-plus`，请求 `https://open.bigmodel.cn/api/paas/v4/chat/completions`。
2. 智谱成功时直接解析 `choices[0].message.content`。
3. 智谱仅在现有 `egress._pre_delivery_failure` 判定为投递前失败时进入 GPT 回退。
4. GPT 回退使用 `OPENAI_API_KEY`、`gpt-4o` 和现有 OpenAI 出境代理链。
5. 智谱超时、连接重置、HTTP 4xx/5xx 或返回无效业务结果时直接失败；上层沿用现有退点逻辑。

智谱地址允许通过 `REVERSE_ZHIPU_BASE` 覆盖，默认值固定为官方 API 根地址。GPT 回退模型允许通过 `BREAKDOWN_FALLBACK_MODEL` 覆盖，默认值为 `gpt-4o`。密钥只从环境变量读取，不写入代码、日志或测试夹具。

## 代码边界

- 修改 `server/content_domains/breakdown.py`：增加智谱主请求、错误分类和 GPT 安全回退；不改变拆解提示词、抽帧、ASR、解析或计费流程。
- 修改 `tests/test_breakdown.py`：增加路由与回退行为测试。
- 不修改前端，不修改 `egress.py` 的作图通道语义，不修改点数文件。

## 日志与错误处理

日志只记录通道名称、模型名和错误类别，不记录 Authorization、API 密钥、图片 base64 或完整提示词。应区分：

- `zhipu success`
- `zhipu pre-delivery failure, fallback to openai`
- `zhipu ambiguous/delivered failure, no fallback`
- `openai fallback failure`

最终异常继续交给现有任务结算逻辑：单条任务失败全退 20 点；批量任务仅退失败链接对应的点数。

## 测试与验收

自动化测试必须覆盖：

1. 智谱请求使用官方智谱路径、智谱密钥和 `glm-4v-plus`，成功时不调用 GPT。
2. 智谱 DNS/连接拒绝/TLS 等投递前失败时调用 GPT，并使用 `gpt-4o` 与 OpenAI 密钥。
3. 智谱 HTTP 404、HTTP 5xx、超时和连接重置时不调用 GPT。
4. GPT 回退失败时异常向上传递，使既有退点流程生效。
5. 现有 `test_breakdown.py`、`test_egress.py` 和计费测试全部通过。

部署后在克隆服务器执行一条提示词反推和一条分镜拆解回归，确认两者成功、日志显示智谱主通道且没有 OpenAI 回退。若需验证回退，只通过自动化测试模拟投递前失败，不在真实接口上人为制造重复计费风险。

## 非目标

- 不取消现有分镜 JSON 解析补偿请求。
- 不修改提示词反推“一次模型请求”的规则。
- 不调整 20 点定价。
- 不新增前端 GPT 回退确认按钮。
