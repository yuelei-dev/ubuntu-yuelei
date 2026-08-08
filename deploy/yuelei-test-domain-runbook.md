# Yuelei 测试域名独立站点发布

目标：让 `yuelei.huangquechuanmei.com` 直接使用测试服务器的静态目录和本地 API，禁止再次代理到 `huangquechuanmei.com` 虚拟主机。

## 发布前

1. 只允许在测试服务器执行，禁止连接生产服务器。
2. 发布对象必须是已合并到 `main` 的精确提交，工作树必须干净。
3. 记录 Nginx PID、配置哈希、域名 health、未登录 401 和关键页面状态。
4. 备份 `/etc/nginx/conf.d/yuelei-canonical-redirect.conf`，记录 SHA-256。

## 安装位置（2026-08-06 实测修正）

渲染产物**不得**放入 `conf.d/`：它引用主站 conf 在 http 级定义的 `huangque_observed`
日志格式与 `hq_cli_upload_*` 限流区，而 `nginx.conf` 先 include `conf.d/*.conf`
后 include `sites-enabled/*`，放 conf.d 会因解析顺序报 unknown log format。
正确布局：

- 正式文件：`/etc/nginx/sites-available/yuelei-test.conf`
- 软链：`/etc/nginx/sites-enabled/yuelei-test.conf`（字母序在主站 huangquechuanmei 之后）
- 同时移除 `/etc/nginx/conf.d/yuelei-canonical-redirect.conf`（备份后再删）

渲染器已去掉 `listen [::]:443 ssl ipv6only=on;` 中的 `ipv6only=on`：该 socket
选项全进程只能声明一次，主站 block 已声明（nginx 对 `[::]` 默认即 ipv6only=on，
行为不变）。

## 生成与验证

从精确合并提交执行：

```bash
python3 deploy/render_yuelei_test_nginx.py \
  --source deploy/nginx-huangquechuanmei.conf \
  --output /tmp/yuelei-canonical-redirect.conf.candidate
```

候选文件必须满足：

- 测试域名使用自己的 TLS 证书；
- 静态根目录为 `/var/www/huangquechuanmei`；
- API 直接代理本机测试服务，并保留 `Host $host`；
- 不包含 `proxy_pass https://127.0.0.1`、生产 Host、生产证书或生产 `proxy_redirect`。

将候选文件放入隔离 Nginx 配置树执行 `nginx -t`，再通过临时回环端口验证首页、登录、工作台、health、401 和全部导航 Location。候选失败时停止，不覆盖正式文件。

## 正式切换

1. 再次确认当前正式文件哈希等于发布前记录；不一致立即停止。
2. 原子写入 `/etc/nginx/sites-available/yuelei-test.conf`，建立
   `sites-enabled/yuelei-test.conf` 软链，并移除
   `/etc/nginx/conf.d/yuelei-canonical-redirect.conf`。
3. 执行 `nginx -t`，通过后仅 `systemctl reload nginx`，不重启六个应用服务。
4. 验证：
   - HTTP 跳转仍为测试域名；
   - 首页、登录和工作台返回 200；
   - 未登录 API 返回 401；
   - `.html` 清理跳转保持测试域名；
   - 页面、JSON、Location 和 Cookie 不产生生产域名；
   - 六个应用服务 PID 和启动时间不变。

## 一键成片用户素材发布顺序

该能力的任务载荷包含 `smart_material_contract_version=1`，旧 worker 不理解用户素材。
发布必须依次执行：

1. 先安装已合并提交中的 8096 后端文件，优雅重启 `huangque-content`，确认
   `/api/gen/health` 正常；
2. 再安装本 runbook 渲染的 Nginx 候选，`nginx -t` 后 reload，验证未登录访问
   `/api/gen/script_to_video/material-upload` 返回 401，而不是 404/413；
3. 最后原子安装 `site/workbench/one-click-video.html`。前两步任一失败都不得发布页面。

回滚时顺序相反：先恢复旧页面阻止新素材任务，再确认所有 v1 任务均已终态，并且
没有尚未写入最终响应的 durable 提交，才能恢复旧后端。以下只读检查的两项都必须
输出 `0`：

```bash
python3 - <<'PY'
import json, sqlite3
db = "/home/ubuntu/content-api/content_jobs.db"
with sqlite3.connect(db) as connection:
    rows = connection.execute(
        "SELECT id,payload FROM jobs WHERE kind='script_to_video' "
        "AND status IN ('pending','running')"
    ).fetchall()
    unfinished_attempts = connection.execute(
        "SELECT COUNT(*) FROM submission_idempotency "
        "WHERE endpoint='/api/gen/script_to_video' AND response_json IS NULL "
        "AND attempt_payload_json IS NOT NULL"
    ).fetchone()[0]
active = [job_id for job_id, raw in rows
          if int(json.loads(raw or "{}").get("smart_material_contract_version") or 0) == 1]
print("active_v1_jobs=%d" % len(active))
print("unfinished_smart_attempts=%d" % int(unfinished_attempts))
PY
```

任一结果非零，就继续使用新后端完成恢复：`linked` 任务需运行到终态并写回响应；
`charged` 提交需用原请求和原幂等键补建任务，或经对账后退款；`frozen` 提交必须先按
`charge_transaction_key` 查询 Auth 账本，确认未扣款后才可清理。禁止直接回滚 worker，
否则旧代码会忽略上传素材并全量调用生图上游，也无法恢复尚未落单的 durable 提交。

## 回滚

任何验证失败时：删除 `sites-enabled/yuelei-test.conf` 软链与
`sites-available/yuelei-test.conf`，用本次发布前备份原子恢复
`/etc/nginx/conf.d/yuelei-canonical-redirect.conf`，执行 `nginx -t` 后 reload，
并重新核对健康、401、页面和服务状态。不得修改数据库、环境变量、点数、任务、
用户文件、Nginx 证书或生产服务器。
