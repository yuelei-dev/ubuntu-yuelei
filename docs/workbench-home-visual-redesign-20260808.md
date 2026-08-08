# 黄雀 AI 工作台首页视觉重设计

## 页面定位

本次目标是登录后的用户工作台首页 `site/workbench/inspiration.html`，不是公开营销落地页。公开首页、登录流程、账户状态、点数、通知、任务、灵感筛选和“一键跟创”等既有业务合同保持不变。

## 视觉规则

1. 桌面采用固定侧栏、顶部状态条和单一主滚动区。
2. 首屏顺序固定为推荐 Hero、四个快捷能力、灵感检索与分类、案例网格。
3. Hero 左侧只承载一个主张与一个主 CTA，右侧使用黄雀原创人物视觉。
4. 深黑和深蓝负责空间层次，暖金只用于当前状态、主按钮和价值提示。
5. 中文标题使用短行和高字重，正文控制行宽，不套用英文站的宽字距。
6. 侧栏当前项同时具有视觉高亮和 `aria-current="page"`。
7. 顶栏只展示真实账户点数、通知、用户入口，以及已登记的创作渠道数量。
8. 快捷卡只指向已经存在的图片、音频、编导和视频工作台。
9. 案例区继续使用黄雀现有灵感数据、分类、搜索、点赞和跟创流程。
10. 动效限定为进入、轻微抬升和焦点反馈，不使用持续抢占注意力的装饰动画。
11. `prefers-reduced-motion`、Save-Data 和低性能设备关闭粒子、连续动画和非必要过渡。
12. 低于 900px 时侧栏转抽屉；手机使用两列快捷卡、单列案例和精简状态栏。

## 参考项目与借鉴边界

| 参考 | 仅借鉴内容 | 许可证记录与边界 |
| --- | --- | --- |
| SPYLT 页面与 `ShowravKormokar/SpyltMilk-clone` | 超大视觉焦点、滚动节奏、克制的高对比动效 | GitHub 未声明 SPDX 许可证；未复制代码、品牌、文案、图片、视频、Logo 或角色资产 |
| `wondelai/skills` top-design | 排版作为结构、留白、叙事节奏、性能优先 | MIT；仅作设计判断资料 |
| `flitzrrr/frontend-design-skills` | 响应式、导航、排版和可访问性检查项 | MIT；仅作规范资料 |
| `aladicf/better-web-ui` | 反模板感自检、层级、色彩语义、动效目的 | 仓库未给出标准 SPDX；未复制代码 |
| `magicuidesign/magicui` | 微弱光晕、细粒子和卡片进入节奏 | MIT；未引入依赖或复制组件 |
| `ibelick/motion-primitives` | 短时进入/退出动画的节制 | MIT；未引入依赖或复制组件 |
| `launch-ui/launch-ui` | Hero、快捷能力、内容发现与 CTA 的转化顺序 | MIT；未引入依赖或复制组件 |
| `adobe/react-spectrum` | 键盘、焦点、ARIA、触控目标和减少动态效果原则 | Apache-2.0；仅作无障碍规范参考 |
| `shadcn-ui/ui` | 控件状态和焦点反馈 | MIT；现有原生 HTML/CSS/JS 技术栈不改写 |
| `Anil-matcha/Open-Generative-AI` | 多生成能力在首页的分层、工具入口与作品入口 | MIT；未复制实现 |
| `mutonby/openshorts` | 视频、短视频、AI 演员入口的流程分组 | GitHub 未识别标准 SPDX；未复制实现 |
| `nodetool-ai/nodetool` | 复杂图片/视频/音频能力的分层方法 | AGPL-3.0；严格不复制代码 |
| `christopherjohnogden/CineGen` | 编导、角色/场景/道具和成片工作区的信息关系 | GitHub 未声明 SPDX；未复制代码 |
| `OpenLoaf/OpenLoaf` | 项目、助手、文件、任务和画布的全局信息架构 | AGPL-3.0；严格不复制代码 |
| `shadcnstore/shadcn-dashboard-landing-template` | 公开页到工作台、账户与任务区域的结构关系 | MIT；未复制组件或模板 |

## 技术决策

- 不新增框架或第三方运行时依赖。
- 新视觉通过原生 CSS 和既有 `cloud-shell.js` 实现。
- Hero 图片为本任务原创生成资产，不包含参考品牌或受版权保护素材。
- 案例图片仍由现有 `inspirations.json` 和后台接口提供，继续懒加载。
