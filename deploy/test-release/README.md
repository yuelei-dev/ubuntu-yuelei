# 统一测试服发布平台：第一阶段

`scripts/release_test.py` 目前只提供统一发布合同、测试机身份、部署 commit 台账、完整运行目录库存、审核拓扑绑定和只读发布计划。它没有 `apply`、`rollback`、`recover`，不会安装运行文件、修改数据库、停止或重启服务，也不会执行功能 PR 自定义命令或访问外部网络。

这是刻意的 fail-closed 边界：独立审核发现旧版本的自由命令、迁移、崩溃恢复和回滚合同尚不能安全证明，因此第一阶段先阻止“功能已合并、发布影响事后再补”的循环；写入型事务执行器必须在后续独立 PR 中完成持久化事务、恢复、备份完整性和进程级故障注入后才能加入。

## 第一阶段解决的问题

- CI 在 PR 的精确 Head 上运行，并断言实际 checkout SHA。
- 功能 PR 修改运行文件时，必须同时新增不可变的 impact 合同。
- CI 同时读取 Base 与 Head catalog，按二者并集识别运行文件。
- `runtime-catalog.json` 是信任根。本 PR 完成首次 bootstrap 后，第一阶段 CI 将它视为不可变；后续映射新增、删除、迁移必须先在独立平台 PR 中设计版本化 catalog 迁移合同，不能直接编辑现有文件，更不能由功能 PR 自我授权。
- 测试机身份同时锁定 `test`、逻辑主机 ID、主机名、`machine-id` 摘要和规范 GitHub origin。
- 台账记录测试服当前对应的精确 `main` commit、全部受管文件哈希和仓库路径。
- 初始化和状态检查会枚举受管前缀，额外旧文件、恶意文件、软链接和未登记文件都会造成漂移。
- `plan` 从 `deployed_main_commit → target main commit` 计算增量，并验证前像、环境变量名称、systemd active、本机回环健康和磁盘空间。
- 相同目标返回 `already_deployed`，不会写台账或触发其他状态变化。
- 审核证据使用 `release_id=MERGE_BASE..HEAD`，Head 必须是目标 main 普通 merge commit 的第二父提交；impact 文件、blob、release ID 和运行文件集合必须与该 PR diff 完全一致。

## 不可绕过的边界

- 脚本不包含 SSH；必须在测试机本地、规范仓库的精确 `main` checkout 中运行。
- Git 固定使用 `/usr/bin/git` 和最小环境；origin 必须精确等于 `https://github.com/yuelei-dev/ubuntu-yuelei.git`。
- HTTP 检查只允许 `127.0.0.1`，不得使用公网域名、查询参数或认证信息。
- 第一阶段 `external_checks` 和 `migrations` 必须是空数组；`no_charge: true` 不能作为执行任意命令的授权。
- catalog 映射迁移、数据库迁移、服务写操作、备份、回滚和崩溃恢复尚不支持；有这些需求的功能 PR 必须在 CI 阶段 fail-closed，等待后续平台能力，而不是手工改服务器。
- 初始化只写私有台账文件；使用内核锁，进程退出后自动释放，不使用会留下 stale lock 的 `O_EXCL` 哨兵锁。

## 功能 PR impact 合同

涉及 catalog 候选目录的 PR，必须在 `deploy/test-release/impacts/` 新增一个 JSON：

- `runtime_changes`：本 PR 修改的运行文件，必须与 Git diff 精确一致。
- `restart_services`：受影响的 allowlisted systemd 单元。
- `required_env`：只写变量名，严禁写值。
- `pre_health_checks`：发布前本机回环健康检查。
- `health_checks`：未来发布完成后的本机回环验收合同。
- `external_checks`：第一阶段必须为 `[]`。
- `migrations`：第一阶段必须为 `[]`。

CI 门禁：

```bash
/usr/bin/python3 scripts/release_test.py check-impact \
  --source-root . \
  --catalog deploy/test-release/runtime-catalog.json \
  --base "$BASE_SHA" \
  --target "$HEAD_SHA"
```

未映射候选文件直接阻断。当前 catalog 在 bootstrap 后不可直接修改；确认文件不是运行时文件或确需新增映射时，必须先扩展并审核版本化 catalog 迁移合同，不能由功能 PR给自己豁免。

## 一次性初始化

合并第一阶段仍不等于操作服务器。后续只能先做只读核对：测试服所有受管文件及目录库存必须完整对应一个已合并 main commit。混合版本、额外文件或缺失文件都会阻断初始化，禁止伪造台账迁就漂移。

```bash
sudo /usr/bin/python3 scripts/release_test.py initialize \
  --deployed-commit "$MAIN_SHA" \
  --confirm-environment test
```

真实 `/etc/huangque/release-identity.json` 权限必须为 `0600`；`/var/lib/huangque-release` 必须为执行账户所有、权限 `0700`。真实 hostname 和 machine-id 摘要不提交到 Git。

## 状态与只读预演

```bash
sudo /usr/bin/python3 scripts/release_test.py status

sudo /usr/bin/python3 scripts/release_test.py plan \
  --target-commit "$MERGED_MAIN_SHA" \
  --reviewed-head "pr-123=$MERGE_BASE_SHA..$REVIEWED_HEAD_SHA"
```

`plan` 只返回 `planned_read_only` 或 `already_deployed`。它不会创建备份、写运行文件、执行迁移、重启服务或改变部署 commit。第二阶段写入型执行器必须复用同一 catalog、台账和审核绑定，且另行通过独立审核。
