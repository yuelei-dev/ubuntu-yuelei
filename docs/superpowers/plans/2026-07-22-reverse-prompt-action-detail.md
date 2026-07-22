# 提示词反推动作细节 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将爆款视频提示词反推升级为包含人物动作、镜头运动及起始—发展—结束时序的六层提示词结构。

**Architecture:** 保持现有 `_reverse_prompt_from_frames` 调用、清洗和返回接口不变，只增强传给多模态模型的用户指令。通过源码级单元测试固定提示词要求，并复用现有调用次数测试保证每个任务仍只请求模型一次。

**Tech Stack:** Python 3.10、`unittest`、现有 `server.content_domains.breakdown` 模块。

## Global Constraints

- 输出仍为一条完整的 500–800 字中文提示词。
- 每个提示词反推任务仍只调用模型一次，不增加补偿重试。
- 不截断、不拼接模型结果；保留现有清洗及空结果错误处理。
- 不修改计费、接口字段、数据库、前端或视频生成接口。
- 本 PR 仅修改 A 组后端文件及关联测试。

---

### Task 1: 六层动作与时序提示词

**Files:**
- Modify: `tests/test_breakdown.py:391-398`
- Modify: `server/content_domains/breakdown.py:216-231`

**Interfaces:**
- Consumes: `_reverse_prompt_from_frames(title, duration, platform, script_text, frames) -> str` 及现有 `_chat_multimodal`、`_clean_reverse_prompt`。
- Produces: 接口不变；模型指令明确六层结构，并包含人物动作、镜头运动及“起始—发展—结束”时序。

- [ ] **Step 1: 写入失败测试**

将 `test_reverse_prompt_requires_structured_detail` 更新为：

```python
def test_reverse_prompt_requires_structured_action_detail(self):
    """反推 prompt 必须要求六层结构、人物/镜头动作、动作时序及 500-800 字。"""
    import inspect
    src = inspect.getsource(self.breakdown._reverse_prompt_from_frames)
    self.assertIn("500-800 字", src)
    self.assertNotIn("150-300 字", src)
    self.assertIn("六个层次", src)
    self.assertIn("动作与时序", src)
    self.assertIn("表情、视线、手势、肢体姿态、走位", src)
    self.assertIn("跟随、推进、拉远、摇移或转场", src)
    self.assertIn("起始—发展—结束", src)
    self.assertIn("镜头（景别、视角、构图和整体运镜风格）", src)
```

- [ ] **Step 2: 运行测试并确认按预期失败**

Run:

```bash
python -m unittest tests.test_breakdown.BreakdownTests.test_reverse_prompt_requires_structured_action_detail -v
```

Expected: FAIL，首个缺失断言为源码中没有 `六个层次`。

- [ ] **Step 3: 最小化修改模型指令**

将 `_reverse_prompt_from_frames` 中五层说明替换为以下六层说明，其他代码保持不变：

```python
"提示词要具体可执行，写清六个层次：①主体（人物/产品的外观、身份、服装和状态）"
"②场景（环境、关键道具、前中后景和空间关系）"
"③动作与时序（按起始—发展—结束描述人物的表情、视线、手势、肢体姿态、走位及与道具的互动，"
"同时写清镜头跟随、推进、拉远、摇移或转场的时机，形成可执行的连续过程，避免‘自然地动起来’等笼统表达）"
"④镜头（景别、视角、构图和整体运镜风格）"
"⑤光线与色调（照明方向、氛围、材质和色彩质感）⑥节奏与情绪钩子。"
"关键帧无法证明的动作不要写成原视频事实；可基于可见信息补充适合原创生成的合理动作，但要保持人物、场景和内容逻辑一致。"
```

保留紧随其后的输出限制：

```python
"直接输出 1 条完整提示词，500-800 字，不要 JSON、不要标题、不要解释、不要 markdown 代码块。"
```

- [ ] **Step 4: 运行定向测试并确认通过**

Run:

```bash
python -m unittest tests.test_breakdown.BreakdownTests.test_reverse_prompt_requires_structured_action_detail tests.test_breakdown.BreakdownTests.test_breakdown_reverse_prompt_calls_model_once tests.test_breakdown.BreakdownTests.test_clean_reverse_prompt_does_not_truncate_long_output -v
```

Expected: 3 tests PASS，0 failures。

- [ ] **Step 5: 运行爆款拆解完整回归**

Run:

```bash
python -m unittest tests.test_breakdown -v
```

Expected: 全部测试 PASS，0 failures，且无新的 warning/error。

- [ ] **Step 6: 检查分组、格式并提交**

Run:

```bash
git diff --check
git diff --name-only
```

Expected: 仅 `server/content_domains/breakdown.py`、`tests/test_breakdown.py` 和本任务文档发生变化；`git diff --check` 无输出。

Commit:

```bash
git add server/content_domains/breakdown.py tests/test_breakdown.py
git commit -m "feat: add action detail to reverse prompts"
```

### Task 2: 资产戳与最终验证

**Files:**
- Verify only: `server/content_domains/breakdown.py`
- Verify only: `tests/test_breakdown.py`

**Interfaces:**
- Consumes: Task 1 完成后的六层提示词实现。
- Produces: 可供代码审核和 PR 提交的干净分支及验证记录。

- [ ] **Step 1: 运行仓库要求的资产戳脚本**

Run:

```bash
python scripts/stamp_assets.py
```

Expected: 命令成功；由于没有前端资产变化，不应产生额外文件修改。

- [ ] **Step 2: 再次运行完整相关测试**

Run:

```bash
python -m unittest tests.test_breakdown -v
```

Expected: 全部测试 PASS，0 failures。

- [ ] **Step 3: 验证单组范围和干净差异**

Run:

```bash
git diff --check origin/main...HEAD
git diff --name-only origin/main...HEAD
git status --short
```

Expected: 差异只包含设计/计划文档、`server/content_domains/breakdown.py` 和 `tests/test_breakdown.py`；不存在未提交源码修改。

