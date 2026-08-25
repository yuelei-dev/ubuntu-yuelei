# 统一测试服发布平台：第一阶段

`scripts/release_test.py` 目前只提供统一发布合同、测试机身份、部署 commit 台账、完整运行目录库存、审核拓扑绑定和只读发布计划。它没有 `apply`、`rollback`、`recover`，不会安装运行文件、修改数据库、停止或重启服务，也不会执行功能 PR 自定义命令或请求任何业务接口。它只会对规范 GitHub origin 做只读版本确认。

这是刻意的 fail-closed 边界：独立审核发现旧版本的自由命令、迁移、崩溃恢复和回滚合同尚不能安全证明，因此第一阶段先阻止“功能已合并、发布影响事后再补”的循环；写入型事务执行器必须在后续独立 PR 中完成持久化事务、恢复、备份完整性和进程级故障注入后才能加入。

## 第一阶段解决的问题

- 常规 CI 在 PR 的精确 Head 上运行并断言实际 checkout SHA；它属于代码回归证据，不单独承担不可绕过的发布影响门禁。
- 独立 `pull_request_target` 工作流只检出受信 Base，使用固定仓库地址把 PR Head 获取为惰性 Git 对象，并且只执行 Base 版本的校验器。工作流自身只有仓库只读权限；唯一密钥是专用 GitHub App 私钥，只交给固定 SHA 的 token action，并仅换取本仓库 Checks write。任何 `run` 块都不能读取该私钥，也不会 checkout 或执行 Head 代码，因此功能 PR 不能通过修改自己的工作流、catalog 或校验器关闭门禁。
- 功能 PR 修改运行文件时，必须同时新增不可变的 impact 合同。
- CI 同时读取 Base 与 Head catalog，按二者并集识别运行文件。
- `runtime-catalog.json` 是信任根。本 PR 完成首次 bootstrap 后，第一阶段 CI 将它视为不可变；后续映射新增、删除、迁移必须先在独立平台 PR 中设计版本化 catalog 迁移合同，不能直接编辑现有文件，更不能由功能 PR 自我授权。
- 测试机身份同时锁定 `test`、逻辑主机 ID、主机名、`machine-id` 摘要和规范 GitHub origin。
- 台账记录测试服当前对应的精确 `main` commit、catalog 的 Git blob/SHA-256、全部已接收 impact，以及每个受管运行文件的哈希、仓库路径、mode、owner 和 group。
- 初始化和状态检查会枚举 catalog 明确声明的完整运行根、精确路径、systemd 单元与 drop-in、Nginx available/enabled 路径、必要软链接及同名前缀邻居，而不是只枚举前缀映射。额外旧文件、恶意文件、软链接和未登记文件都会造成漂移；`__pycache__` 不再获得豁免，包括 unchecked-hash `.pyc`。数据库、环境文件和产物目录只能通过带 kind、owner、group、allowed mode 与 required 标志的类型化运行数据合同排除。
- `plan` 从 `deployed_main_commit → target main commit` 计算增量，并从每个 systemd 单元真实声明的 `EnvironmentFile`/`Environment` 合同验证变量名，不信任发布器进程环境；同时验证精确前像、active/loaded 服务状态、命名健康合同和磁盘空间。返回计划前会再次核验身份、台账、checkout/live main、catalog、已接收 impact、完整运行库存、全部受管文件和服务状态。第一阶段只报告待验收探针，不请求对应接口。
- 相同目标返回 `already_deployed`，不会写台账或触发其他状态变化。
- 审核证据使用 `release_id=MERGE_BASE..HEAD`，Head 必须是目标 main 普通 merge commit 的第二父提交，且 `MERGE_BASE` 必须精确等于 merge commit 的第一父提交；impact 文件、blob、release ID、运行文件集合和 merge 后实际运行文件内容必须与审核 Head 一致。

## 不可绕过的边界

- 脚本不包含 SSH；运行命令只能从 root 安装的固定启动器 `/usr/local/sbin/huangque-release-test` 启动，数据源只能是 root 管理的规范镜像 `/opt/huangque-test-release`。启动器在 Python 运行前校验固定入口 SHA-256，以 `env -i` 和 `/usr/bin/python3 -I -E -s -B` 启动解释器；脚本再核验固定 `sys.executable`、启动器、入口、manifest、系统 CA、镜像及其每一级父目录的 root 属主、不可写权限和精确哈希。禁止通过 `sudo` 直接执行 checkout 或已安装的 Python 文件。
- Git 固定使用 `/usr/bin/git` 和最小环境；禁用 replace、grafts、alternates、shallow、fsmonitor、hooks、credential helper、askpass、URL rewrite、代理、SSL 降级和可写 optional locks。规范 GitHub `main` 通过不使用系统或环境代理的固定 HTTPS API 与固定 `/etc/ssl/certs/ca-certificates.crt` 独立读取，不读取 `SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE`、`SSLKEYLOGFILE` 或代理变量，也不使用本地 Git transport 配置。
- 功能 PR 只能引用不可变 catalog 中的命名健康探针，不能提交 URL、端口或路由；第一阶段 `plan` 不发送这些 HTTP 请求。
- 生产 CLI 固定使用 `deploy/test-release/runtime-catalog.json`、`/etc/huangque/release-identity.json`、`/var/lib/huangque-release` 和 `/` 运行根；这些信任根不能通过命令行替换。测试注入只允许通过 Python 构造器。
- 第一阶段 `external_checks` 和 `migrations` 必须是空数组；`no_charge: true` 不能作为执行任意命令的授权。
- catalog 映射迁移、数据库迁移、服务写操作、备份、回滚和崩溃恢复尚不支持；有这些需求的功能 PR 必须在 CI 阶段 fail-closed，等待后续平台能力，而不是手工改服务器。
- nginx 配置在第一阶段被明确标记为 `merged_not_releasable`，因为安全发布还需要语法验证与事务化 reload；systemd 变更只在只读计划中输出 `daemon_reload_required`，不会实际执行 reload。
- 初始化只写私有台账文件；使用内核锁，进程退出后自动释放，不使用会留下 stale lock 的 `O_EXCL` 哨兵锁。

## Base 所有的发布门禁

`.github/workflows/release-impact-gate.yml` 是未来功能 PR 的权威发布影响门禁。它在 `pull_request_target` 上运行，但只授予 `contents: read` 与 `pull-requests: read`，只执行 Base checkout 中的 `scripts/release_test.py`；Head 仅作为 Git 数据供 Base 校验器读取。

PR #294 是该门禁的一次性 bootstrap：它的 Base 尚不存在这份工作流，所以本 PR 必须依靠精确 Head 常规 CI 和独立复审完成启动审核。合并本 PR 本身不等于门禁已经生效。

当前仓库属于 personal account，不能使用仅支持 organization/enterprise ruleset 的 **Require workflows to pass before merging**，也不能把 `pull_request_target` 自身的 Base SHA job 误配成 Head required check。Base-owned 工作流因此只负责校验，并使用专用 GitHub App 在精确 PR Head SHA 上发布名为 `Trusted test release impact gate` 的 Check Run。

首次启用需要仓库管理员另行创建并只安装到本仓库的专用 GitHub App。该 App 只授予 Metadata read 与 Checks read/write，不授予 Contents、Actions、Administration 或其他写权限；把 App ID 存为 Actions variable `TEST_RELEASE_GATE_APP_ID`，私钥存为 secret `TEST_RELEASE_GATE_APP_PRIVATE_KEY`。随后将 `Trusted test release impact gate` 配置为 required status check，expected source 必须锁定为这个专用 App，并启用 strict/up-to-date；如启用 merge queue，也必须要求该检查在最新 merge-group SHA 上通过。当前工作流尚未处理 `merge_group`，所以在补齐并审核该事件前不得启用 merge queue。

启用后必须做两项真实演练：一是新增同名 GitHub Actions job，确认它不能冒充专用 App 的检查；二是在检查变绿后推进 Base，确认旧 Head/Base 结果失效并重新要求最新校验。App、secret、required check 和两项演练全部完成前，后续运行时功能 PR 必须保持 `merged_not_releasable`。PR #294 不创建 App、不写 secret、不修改仓库规则。

Base 校验器还会拒绝 bootstrap 后任何 `.github/workflows/**` 增删改，防止普通功能 PR新增同名检查冒充门禁。未来工作流治理变更必须走新的受控 bootstrap；不能与业务运行文件混在同一 PR 中。

第一阶段把以下信任根视为不可变：常规 CI、Base-owned gate、统一校验器和 runtime catalog。后续若要升级平台，必须作为新的受控 bootstrap 独立审核并同步切换 required check，不能与业务运行文件混在同一功能 PR 中。

## 功能 PR impact 合同

涉及 catalog 候选目录的 PR，必须在 `deploy/test-release/impacts/` 新增一个 JSON：

- `runtime_changes`：本 PR 修改的运行文件，必须与 Git diff 精确一致。
- `restart_services`：受影响的 allowlisted systemd 单元。
- `required_env`：按受影响服务写成 `{"unit.service": ["VARIABLE_NAME"]}`，只写变量名，严禁写值；服务必须同时出现在精确 `restart_services` 中。
- `pre_health_checks`：catalog 中命名的发布前健康合同 ID。
- `health_checks`：catalog 中命名的发布后验收合同 ID。
- `external_checks`：第一阶段必须为 `[]`。
- `migrations`：第一阶段必须为 `[]`。

CI 门禁：

```bash
/usr/bin/python3 -I -E -s -B scripts/release_test.py check-impact \
  --source-root . \
  --base "$BASE_SHA" \
  --target "$HEAD_SHA"
```

未映射候选文件直接阻断。当前 catalog 在 bootstrap 后不可直接修改；确认文件不是运行时文件或确需新增映射时，必须先扩展并审核版本化 catalog 迁移合同，不能由功能 PR给自己豁免。

bootstrap 会扫描目标 commit 中全部候选文件，而不只扫描本 PR diff；当前清单审计结果必须是 0 个未分类候选。`server/nginx-huangquechuanmei.conf` 是已废弃的重复配置，规范来源是 `deploy/nginx-huangquechuanmei.conf`；`worker/worker.py` 是 Mac 端程序，`worker/server_worker.py` 不属于当前测试机批准的 systemd 运行集合，因此均显式列入 ignore。它们将来若进入测试服受管运行集合，必须先通过独立的版本化 catalog 迁移 PR，不能由功能 PR直接启用。

catalog 允许一个仓库文件映射到多个运行位，例如 leadgen A/B 双槽、Nginx available/enabled 及 content/auth 共享模块；也允许一个运行文件声明多个受影响服务，例如 `func_names.py` 同时要求 admin 与 content，`tikhub.py` 必须同时要求 content、leadgen-api 与 imggen-api 的健康合同。台账按每一个实际运行路径记录哈希，不能用单一源码路径掩盖 fan-out。

当前 catalog 同时交叉校验 `drift_sentinel.py` 的后端权威映射，并覆盖其列出的三项服务器脚本；`scripts/pool_health.py` 也显式 fan-out 到 leadgen A/B。Hermes 的 `prompt.md`、`README.md` 和 migration helper 已按权威部署脚本纳入受管映射。`content-api/.deploy/whisper-runtime-20260815/`、Hermes `backups/`、日志、cookies、`*.bak*`、`*_backup.py` 和其他历史文件没有获得目录级豁免：在完成真实只读盘点、逐项证明为运行数据或经授权退役前，它们会作为未分类文件阻断初始化。不能用宽泛前缀掩盖可执行历史产物。

## 一次性初始化

合并第一阶段仍不等于操作服务器。后续只能先做只读核对：测试服所有受管文件及目录库存必须完整对应一个已合并 main commit。混合版本、额外文件或缺失文件都会阻断初始化，禁止伪造台账迁就漂移。

首次服务器 bootstrap 是独立的管理员操作，不由功能 PR 或本 PR 自动执行。管理员必须从已审核并合并的精确 commit 制作产物：把 `scripts/release_test_launcher.sh` 安装为 root:root、`0755` 的 `/usr/local/sbin/huangque-release-test`，把 `scripts/release_test.py` 安装为 root:root、`0755` 的 `/usr/local/libexec/huangque-release/release_test.py`，把二者精确 SHA-256 写入 `deploy/test-release/bootstrap.example.json` 后安装为 root:root、`0600` 的 `/etc/huangque/release-bootstrap.json`，并把完整仓库镜像安装到 root:root、部署账户不可写的 `/opt/huangque-test-release`。启动器路径只能在 sudoers 中按固定命令授权，不能授予部署账户任意 root Python。镜像必须是完整历史且全部文件及 `.git` 由 root 持有、group/other 不可写，不得含 symlink、replace refs、grafts、alternates、shallow 元数据或隐藏执行/路由 Git 配置。初始化前还必须清除运行根中的全部 `__pycache__`/`.pyc`，并在后续独立 systemd 硬化 PR 中为会导入可写源码的 Python 服务设置 `PYTHONDONTWRITEBYTECODE=1`；在该 PR 合并部署并复审以前，平台保持 fail-closed，不允许伪造库存通过。安装、身份文件和真实 inventory 核对都需要另行授权；未满足任一项时运行命令必须 fail-closed。

```bash
sudo /usr/local/sbin/huangque-release-test initialize \
  --deployed-commit "$MAIN_SHA" \
  --confirm-environment test
```

真实 `/etc/huangque/release-identity.json` 权限必须为 `0600`；`/var/lib/huangque-release` 必须为执行账户所有、权限 `0700`。真实 hostname 和 machine-id 摘要不提交到 Git。

## 状态与只读预演

```bash
sudo /usr/local/sbin/huangque-release-test status

sudo /usr/local/sbin/huangque-release-test plan \
  --target-commit "$MERGED_MAIN_SHA" \
  --reviewed-head "pr-123=$MERGE_BASE_SHA..$REVIEWED_HEAD_SHA"
```

`plan` 只返回 `planned_read_only` 或 `already_deployed`。它不会创建备份、写运行文件、执行迁移、重启服务或改变部署 commit。第二阶段写入型执行器必须复用同一 catalog、台账和审核绑定，且另行通过独立审核。
