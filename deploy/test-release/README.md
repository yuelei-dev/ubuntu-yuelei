# 统一测试服发布平台

`scripts/release_test.py` 是后续测试服发布的唯一通用执行器。历史锁定清单和旧执行器暂时保留为只读兼容路径；迁移完成前不得用新执行器重放旧清单，也不得为了启用本平台手工覆盖业务运行文件。

## 解决的问题

旧发布方式把服务器某一时刻的文件哈希写进每个功能 PR。另一个 PR 先上线后，后续清单的前像立即过期。新方式以测试服台账中的 `deployed_main_commit` 为起点，以已合并的目标 `main` commit 为终点，直接从 Git 计算运行文件的前像和后像。实时文件不等于 Git 前像时仍然 fail-closed，但正常连续发布不再制作混合前像清单。

## 不可绕过的边界

- 执行器不包含 SSH，也不会连接远程主机；必须在目标测试机本地、精确的 `main` checkout 中运行。
- `apply` 同时要求干净的 `main`、`HEAD == origin/main == 目标 commit`、实时远端 `main` 一致，以及独立审核 Head 已包含在目标 commit。
- 身份文件同时锁定 `test`、逻辑主机 ID、主机名和 `/etc/machine-id` 的 SHA-256。
- 所有工具、环境变量名称、运行文件、systemd 单元、迁移、无扣点外部检查和回环健康检查都由数据化发布影响声明控制。
- 环境变量只检查是否存在，永不输出值。外部预检必须明确 `no_charge: true`。
- HTTP 验收只允许 `127.0.0.1`，不使用可能命中其他机器的公网域名。
- 写入前验证整本台账、命令、权限、磁盘、外部依赖和待改文件前像；任一失败都不会创建备份或触碰运行文件。
- SQLite 迁移先做一致性快照，并在服务停止后执行；失败时恢复快照、文件和旧台账。
- 重复部署同一目标返回 `already_deployed`，不备份、不写文件、不重启。
- 并发 `apply`/`rollback` 由独占锁拒绝。
- 同一批次包含多个功能 PR 时，每个 `release_id` 都必须提供自己的精确审核 Head；多个 PR 连续修改同一运行文件是允许的，最终后像仍由目标 main commit 唯一确定。

## 功能 PR 合同

涉及 `runtime-catalog.json` 候选目录的 PR，必须在 `deploy/test-release/impacts/` 新增一个 JSON。字段含义：

- `runtime_changes`：本 PR 真正要安装的仓库文件，必须与 Git diff 精确相等。
- `restart_services`：需要停止/启动或重启的 systemd 单元；目录映射要求的单元不能遗漏。
- `required_env`：只写变量名，不写密钥值。
- `health_checks`：至少一个本机回环 HTTP 验收。
- `pre_health_checks`：服务变更前必须通过的本机回环健康检查；回滚后也用它验收旧版本。
- `external_checks`：可选、必须明确无扣点，命令必须来自允许列表。
- `migrations`：可选；当前只接受 `restore_sqlite_snapshot` 回滚策略。

CI 对 PR base/head 运行：

```bash
python scripts/release_test.py check-impact \
  --source-root . \
  --catalog deploy/test-release/runtime-catalog.json \
  --base "$BASE_SHA" \
  --target "$HEAD_SHA"
```

未映射的候选文件会直接阻断。确认某个文件不是运行时文件时，必须在 catalog 中显式忽略并接受审核，不能靠文件名猜测跳过。

## 一次性初始化

第一阶段只合并仓库能力，不操作服务器。后续单独执行测试服上线时：

1. 只读确认测试服实际运行状态能完整对应一个已合并的 `main` commit。
2. 由运维在测试机创建 `/etc/huangque/release-identity.json`，权限 `0600`；真实主机指纹不进 Git。台账目录 `/var/lib/huangque-release` 必须由执行账户持有且权限为 `0700`。
3. 测试机源码仓库快进到同一个 commit，确保工作区干净且远端一致。
4. 运行 `initialize`。它逐个比较 catalog 映射文件与该 commit；只要有一个不一致就不建立台账。

```bash
sudo /usr/bin/python3 scripts/release_test.py initialize \
  --deployed-commit "$MAIN_SHA" \
  --confirm-environment test
```

如果当前服务器是混合版本，初始化必须失败。应先通过仍受审核的旧发布事务把运行时对齐到一个 main commit，再初始化；禁止伪造台账来迁就漂移。

## 预演、发布、状态与回滚

```bash
sudo /usr/bin/python3 scripts/release_test.py plan \
  --target-commit "$MERGED_MAIN_SHA"

sudo /usr/bin/python3 scripts/release_test.py apply \
  --target-commit "$MERGED_MAIN_SHA" \
  --reviewed-head "pr-123=$REVIEWED_HEAD_SHA" \
  --confirm-environment test

sudo /usr/bin/python3 scripts/release_test.py status

sudo /usr/bin/python3 scripts/release_test.py rollback \
  --release-id "$RELEASE_ID" \
  --confirm-environment test
```

`rollback` 只允许回滚当前最后一次成功发布。备份和记录默认保存在 `/var/lib/huangque-release`；清理策略应在真实测试服稳定运行后另行审核，第一阶段不自动删除审计证据。
