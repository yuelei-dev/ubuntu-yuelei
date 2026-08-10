# 首页整页迁移视觉证据

- Base：`yuelei-dev/ubuntu-yuelei@209b8a77141c78ba7d81613d33b5a128dde1c6f4`
- Head：见本 PR 最终提交
- 本地服务：静态 `site/`，未连接测试或生产服务器

## 对照截图

- `before-yue-main-hero-1440x900.png`：Yuelei `main` 的月球 Hero
- `before-yue-main-samples-1440x900.png`：Yuelei `main` 的旧样片区域
- `after-desktop-hero-1440x900.png`：Tang 双视频眼睛 Hero
- `after-desktop-gallery-deep-arc-1440x900.png`：最终加深下凹的环形画廊
- `after-mobile-hero-390x844.png`：手机 Hero 与折叠菜单
- `after-mobile-gallery-deep-arc-390x844.png`：手机画廊降级布局

## 浏览器验收记录

- 桌面：1440×900，21 张卡片、1 张活动卡片，无横向溢出；最终可见下凹高度差约 147px。
- 手机：390×844，21 张卡片、1 张活动卡片，无横向溢出；画廊高度 500px，下凹高度差约 28px。
- 交互：拖拽后活动样片发生变化；`ArrowRight` 切换样片；`Enter` 打开预览；关闭按钮恢复画廊焦点。
- 资源：Hero 保留 2 个本地视频与全页粒子 canvas；画廊只有中心视频加载和播放。
- 控制台：本地验收期间 error/warn 为 0。
