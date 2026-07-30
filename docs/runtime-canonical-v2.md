# 测试服务器 Git 正本 v2

## 不可越过的边界

- 生产 `129.204.166.13` 永远不参与采集、比较、发布或回滚。
- 测试 `8.148.158.106` 的采集阶段只读；本 PR 不部署、不切换、不重启。
- 本方案从 `main` 独立派生，不以 `runtime/biandao-141-142` 或 PR #146 为 base，
  不 merge/cherry-pick/rebase 它们，也不修改它们的文件。
- 当前 manifest 的 `capture_kind=repository-candidate`，不是测试机实采结果。只有由只读
  采集会话生成且标记 `server-read-only` 的 manifest 才能进入未来发布审批。

## 冻结范围

`scope.json` 用允许根目录和允许后缀定义代码、静态站和启动配置的边界，并强制排除：
env、密钥、证书、数据库、`content_out`、上传与生成物、日志、缓存和用户数据。
任何符号链接、疑似凭据内容、重复/越界路径都会使采集失败。

服务器实采必须先用 `capture_test_runtime_readonly.sh` 将允许范围以只读 tar 流导出
到 `D:\缓存区`。脚本只接受 `8.148.158.106`，强制 BatchMode、host-key 校验、关闭
转发，并在远端选择阶段排除 env、证书、数据库、日志、备份和生成数据；不使用 sudo，
也不创建远端文件。systemd 使用明确 allowlist，并在传输阶段脱敏 `Environment=`；
nginx 使用只读 `nginx -T` 输出，不复制证书或私钥。随后在本地解包并运行：

```bash
python scripts/runtime_canonical.py \
  --scope deploy/runtime-canonical/scope.json \
  inventory --source SNAPSHOT_ROOT \
  --output baseline-server.json \
  --capture-kind server-read-only \
  --source-revision 'test:8.148.158.106@UTC_TIMESTAMP' \
  --mode-map CAPTURED_TAR_MODE_MAP.json
```

## 不可变 release

manifest 的 `content_id` 是除自身外整个规范化 manifest 的 SHA-256。构建器逐文件复核
哈希后复制到 `releases/<content_id>`；目标已存在就失败，禁止覆盖。切换只允许把
`current` 符号链接通过同目录 `rename(2)` 原子替换。初始化是独立操作；之后激活和
回滚都强制提供 `EXPECTED_OLD_ID`，并由 `flock` 覆盖读取、比较和替换全过程。切换前
必须重新验证 server-verified manifest、目录 content ID、全部文件哈希/模式和额外
文件。回滚与发布使用同一原子原语，目标只能是已有且已验证的 `content_id`。

未来服务器写入窗口的顺序固定为：

1. 本地和 CI 验证 server-verified manifest。
2. 将 release 放入新的 content-id 目录；绝不覆盖旧目录。
3. 离线验证 release、依赖和健康检查命令。
4. 记录旧 content-id，以 CAS 原子切换 `current`。
5. 重启/重载与健康检查必须另行明确审批；失败立即 CAS 回滚旧 content-id。

本 PR 只提供原语，不执行上述任何服务器操作。

## CI 阻断语义

workflow 没有 `continue-on-error`、容错后缀或可跳过条件，并拆为三个独立门禁：
`canonical-unit-gates`、`server-baseline-integrity`、`pr146-isolation`。缺少真实
`baseline-server.json` 时第二个 job 必须失败；#146 隔离 job 动态读取它的当前 head
与 base，不维护易过期的手写文件列表。三个 job 都必须配置成 required status check；
未设置前不能宣称治理已生效。

## 当前只读证据

2026-07-30 已通过只读 SSH 对 `8.148.158.106` 完成实采。原始业务/静态归档
SHA-256 为 `5ae75ed2f397d0147211378bbdd0188de9536cb7a023ab8c91cd341058491abe`；
远端读取阶段没有纳入 env、密钥、证书、数据库、备份、日志、上传、生成物或用户数据。
完整脱敏 payload 存放在 `runtime-baselines/test/<content-id>/`，不覆盖 live source
路径，不部署、不切换，也不作为 PR #146 的 base。
