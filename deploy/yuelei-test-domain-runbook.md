# Yuelei 测试域名独立站点发布

目标：让 `yuelei.huangquechuanmei.com` 直接使用测试服务器的静态目录和本地 API，禁止再次代理到 `huangquechuanmei.com` 虚拟主机。

## 发布前

1. 只允许在测试服务器执行，禁止连接生产服务器。
2. 发布对象必须是已合并到 `main` 的精确提交，工作树必须干净。
3. 记录 Nginx PID、配置哈希、域名 health、未登录 401 和关键页面状态。
4. 备份 `/etc/nginx/conf.d/yuelei-canonical-redirect.conf`，记录 SHA-256。

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
2. 原子替换测试域名配置。
3. 执行 `nginx -t`，通过后仅 `systemctl reload nginx`，不重启六个应用服务。
4. 验证：
   - HTTP 跳转仍为测试域名；
   - 首页、登录和工作台返回 200；
   - 未登录 API 返回 401；
   - `.html` 清理跳转保持测试域名；
   - 页面、JSON、Location 和 Cookie 不产生生产域名；
   - 六个应用服务 PID 和启动时间不变。

## 回滚

任何验证失败时，立即用本次发布前备份原子恢复配置，执行 `nginx -t` 后 reload，并重新核对健康、401、页面和服务状态。不得修改数据库、环境变量、点数、任务、用户文件、Nginx 证书或生产服务器。
