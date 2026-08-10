# Tang 首页视觉前移记录（2026-08-10）

## 目标与来源

- 目标仓库：`yuelei-dev/ubuntu-yuelei`
- 页面基线：`tang730125633/huangque-main-site@9a21d8af47deb2298e878dec384f48c42ac0dbeb`
- 本次本地整合快照：`9adb8e9a47ca1cf97ef3e942fa8a5f461c3d97e8`
- 用户确认范围：保留 Tang 基线首页的整页结构、导航、眼睛双视频 Hero、液态玻璃与滚动粒子叙事，仅将原三张 `CREATIVE OUTPUT` 卡片替换为环形作品画廊。

这是页面级前移，不是仓库覆盖。未迁入 Tang 的工作台、后端、部署配置、数据库或运行数据。

## 文件边界

- 首页结构与样式：`site/index.html`、`site/homepage.css`
- 首页交互：`site/homepage-liquid-glass.js`、`site/homepage-particles.js`
- Hero 与粒子资源：两个本地 MP4、两份点云二进制文件
- 环形画廊：独立 JavaScript、清单、许可证、8 张图片、13 段视频及其静态封面
- 回归测试：首页视频、旧月球替换哨兵、环形画廊合同

## 版权与依赖边界

- 首页页面及 Hero 资源均来自上述黄雀 Tang 仓库精确提交，没有从 SPYLT 或其他外部参考站点复制品牌、文案、Logo、人物或媒体。
- 环形画廊代码是本任务的原生实现，许可证随 `site/assets/home/orbit-gallery.LICENSE.txt` 提交。
- 画廊媒体来自用户确认的本地黄雀样片集合，不包含运行时远程依赖。
- 没有引入 npm、Python 或浏览器第三方库；页面继续使用仓库已有字体、Logo、图标和工作台路由。

## 回退

回退时只需 revert 本 PR 的页面级提交。后端、工作台和数据结构未变，不需要迁移、环境变量或服务重启。
