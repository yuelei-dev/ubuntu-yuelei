# 果肉视频 OpenRouter 安全备用通道设计

## 背景

克隆服务器的果肉视频目前只调用 xAI 官方接口。xAI 团队额度耗尽或达到月度消费上限时，创建请求返回 HTTP 403，任务失败并退点。主服务器已通过主站 PR #721、#722 增加 OpenRouter Grok Video 备用通道，克隆服务器尚未同步。

## 目标

- 保持 xAI 为果肉视频主通道。
- xAI 在创建任务前明确返回鉴权或额度错误时，安全回退 OpenRouter。
- 支持 OpenRouter 文生视频、图生视频、任务轮询、重启恢复和鉴权下载。
- 防止创建结果不确定或任务已创建后再次提交，避免双重计费。
- 保持现有每秒点数、失败退点、前端交互和其他视频渠道不变。
- 仅修改并部署克隆服务器，不修改主服务器。

## 方案选择

采用完整移植主站 #721 与 #722 的方案。相比全量切换 OpenRouter，它保留 xAI 主通道；相比只捕获 HTTP 403 的临时处理，它同时覆盖任务恢复、下载鉴权和非幂等安全边界。

## 架构与数据流

1. `video.py` 收到果肉文生或图生视频任务，优先调用 `video_xai.generate`。
2. `video_xai` 只把创建前确定的 401、402、403 或缺少 xAI 密钥包装为 `XaiCreateUnavailableError`。
3. `video.py` 仅捕获 `XaiCreateUnavailableError`；若 OpenRouter 已配置，则调用 `video_openrouter.generate`。
4. OpenRouter 创建成功后立即保存任务 ID 和 `openrouter_*` 阶段，再进入轮询。
5. 服务重启时，`startup_recovery.py` 与 `get_resumable_grok_request` 根据阶段识别原提供商，并从同一提供商恢复轮询，绝不重新创建。
6. OpenRouter 完成后返回受保护的结果地址，下载时仅给原始 OpenRouter 地址附加 Bearer 鉴权；代理或其他候选下载地址不得携带该密钥。

果肉视频编辑继续使用 xAI 官方接口，不自动回退 OpenRouter。xAI 网络超时、连接中断、未知创建结果、已获得任务 ID 后的轮询或下载失败均不得触发 OpenRouter 创建。

## 配置与密钥

克隆服务器增加以下运行时配置：

- `OPENROUTER_API_KEY`：从主服务器安全复制到克隆服务器的服务环境文件。
- `OPENROUTER_API_BASE=https://openrouter.ai/api/v1`
- `OPENROUTER_VIDEO_TIMEOUT=1200`
- `OPENROUTER_VIDEO_POLL_INTERVAL=10`

密钥不写入仓库、提交记录、日志、测试或最终报告。主服务器只读，不修改配置或服务。

## 代码范围

- 新增 `server/content_domains/video_openrouter.py`。
- 修改 `server/content_domains/video_xai.py`、`server/content_domains/video.py`、`server/content_domains/startup_recovery.py`。
- 新增或修改 `tests/test_video_openrouter.py`、`tests/test_video_xai.py`、`tests/test_xiaole_video.py`。
- 更新不含真实密钥的示例配置与密钥文档；若克隆仓库的单组规则不允许文档同 PR，则文档留在本设计与部署记录中，不扩大业务代码范围。

不修改前端、定价、点数结算、数据库结构或非果肉视频渠道。

## 错误处理与计费安全

- xAI 创建前 401/402/403：允许回退 OpenRouter。
- xAI 创建 POST 的网络错误或超时：结果不确定，禁止回退。
- xAI 已返回任务 ID：禁止回退，持续轮询或按原错误失败。
- OpenRouter 创建 POST 的网络错误或超时：结果不确定，禁止重试创建。
- OpenRouter 轮询的 408/429/5xx 或网络错误：只重试 GET，不重复创建。
- 两个提供商均不可用：异常上抛，沿用现有退点流程。

日志只记录提供商、任务 ID、状态和错误类别，不记录密钥、完整素材数据或鉴权头。

## 测试与验收

自动化测试覆盖：

1. xAI 401/402/403 在创建前转换为可安全回退错误。
2. xAI 网络未知结果、轮询和下载失败不触发 OpenRouter 创建。
3. OpenRouter 文生与图生请求参数、模型映射、鉴权和安全 URL 校验正确。
4. OpenRouter 创建只提交一次，轮询瞬时错误只重试 GET。
5. OpenRouter 任务阶段持久化并能在服务重启后恢复。
6. OpenRouter 结果下载只对原始地址附加鉴权，密钥不进入代理候选请求。
7. 现有果肉视频、计费、退点和模块门禁测试全部通过。

部署到克隆服务器后，先验证服务、环境变量存在性和模块导入，再使用当前 xAI 额度失败条件提交一条低风险果肉生成任务。验收标准是日志显示 xAI 创建前失败后切换 OpenRouter、OpenRouter 只创建一次、任务完成并成功下载成片；若 OpenRouter 账户本身不可用，则任务应失败退点且不发生重复提交。

## 回滚

部署前备份四个后端文件和环境文件。出现回归时恢复文件与环境备份、重启 `huangque-content.service`，即可回到 xAI 单通道行为。
