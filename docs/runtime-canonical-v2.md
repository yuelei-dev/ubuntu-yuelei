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

服务器实采必须先将允许范围以只读方式导出到隔离暂存目录；导出工具不得在服务器创建
文件。随后在本地运行：

```bash
python scripts/runtime_canonical.py \
  --scope deploy/runtime-canonical/scope.json \
  inventory --source SNAPSHOT_ROOT \
  --output baseline-server.json \
  --capture-kind server-read-only \
  --source-revision 'test:8.148.158.106@UTC_TIMESTAMP'
```

## 不可变 release

manifest 的 `content_id` 是除自身外整个规范化 manifest 的 SHA-256。构建器逐文件复核
哈希后复制到 `releases/<content_id>`；目标已存在就失败，禁止覆盖。切换只允许把
`current` 符号链接通过同目录 `rename(2)` 原子替换，并用 `EXPECTED_OLD_ID` 做 CAS，
防止并发或陈旧审批覆盖。回滚与发布使用同一原子原语，目标只能是已有且已验证的
`content_id`。

未来服务器写入窗口的顺序固定为：

1. 本地和 CI 验证 server-verified manifest。
2. 将 release 放入新的 content-id 目录；绝不覆盖旧目录。
3. 离线验证 release、依赖和健康检查命令。
4. 记录旧 content-id，以 CAS 原子切换 `current`。
5. 重启/重载与健康检查必须另行明确审批；失败立即 CAS 回滚旧 content-id。

本 PR 只提供原语，不执行上述任何服务器操作。

## CI 阻断语义

workflow 没有 `continue-on-error`、容错后缀或可跳过条件。单元测试、manifest 结构、
逐文件哈希、范围规则和 #146 文件隔离任一失败，job 即失败。仓库管理员仍需把
`runtime-canonical-v2 / canonical-gates` 配为 `main` 的 required status check；
这项 GitHub 分支保护设置不在本地 PR 权限范围内，未设置前不能宣称治理已生效。

## 当前证据缺口

2026-07-30 的只读 SSH 尝试对 `root@8.148.158.106` 和
`ubuntu@8.148.158.106` 均被认证拒绝。因此本 PR 不能诚实地产生
`server-read-only` baseline。候选 manifest 只证明工具链可在 `main` 的运行时
映射上完整工作；CI 的 server-verified gate 必须在取得只读凭据并完成实采后才启用，
且启用后不得降级或跳过。
