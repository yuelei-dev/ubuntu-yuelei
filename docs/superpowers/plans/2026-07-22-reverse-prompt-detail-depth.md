# 提示词反推详细度增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让提示词反推以一次模型请求消费 8 个高清时间点、4 张时序双帧图和 1800 输出 tokens，并通过六层最低细节数提高结果详细度。

**Architecture:** 在现有拆解模块内增加反推专用抽帧与拼图路径，不改变普通分镜拆解。共享多模态调用增加可选的图片 detail 与 `max_tokens` 参数，反推省略未被智谱文档确认的 detail 字段并显式设置 1800 tokens。

**Tech Stack:** Python 3.10、标准库、FFmpeg、`unittest`、智谱 OpenAI 兼容 Chat Completions。

## Global Constraints

- 每个反推任务仍只调用模型一次。
- 不增加不足 500 字提示、字数失败判定、自动补写或补偿重试。
- 不修改前端、接口字段、数据库、模型路由、20 点计费或退款规则。
- 普通分镜拆解保持当前抽帧数量、512 像素和 `detail: low`。
- 智谱输入不超过 5 张图片；反推将 8 帧组合为 4 张双帧图。
- 本 PR 仅修改 A 组后端与关联测试。

---

### Task 1: 反推专用 8 帧高清时序拼图

**Files:**
- Modify: `tests/test_breakdown.py`
- Modify: `server/content_domains/breakdown.py:104-145,361-397`

**Interfaces:**
- Consumes: `_extract_frames(video_path, count, duration, scale_width=512) -> (str, list[str])`。
- Produces: `_pair_reverse_frames(frame_dir, frames) -> list[str]`，严格接收 8 帧并返回按时间顺序排列的 4 张横向双帧图。

- [ ] **Step 1: 写入失败测试**

增加以下测试：

```python
def test_reverse_mode_extracts_eight_high_resolution_frames_and_pairs_them(self):
    calls = self._install_fake_env("反推结果", transcript=[])
    def fake_extract(path, count, duration, scale_width=512):
        calls["extract_args"] = (count, scale_width)
        return "frames-dir", ["f%d.jpg" % i for i in range(1, 9)]
    def fake_pair(frame_dir, frames):
        calls["pair_args"] = (frame_dir, list(frames))
        return ["p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"]
    self.breakdown._extract_frames = fake_extract
    self.breakdown._pair_reverse_frames = fake_pair
    self.breakdown._chat_multimodal = lambda *args, **kwargs: "反推结果"
    result = self.breakdown._do_breakdown(
        {"_job_id": 80, "mode": "reverse_prompt"},
        {"platform": "douyin", "id": "detail-depth"},
        "https://example.test/detail-depth",
        "reverse_prompt",
    )
    self.assertEqual(calls["extract_args"], (8, 1024))
    self.assertEqual(calls["pair_args"][1], ["f%d.jpg" % i for i in range(1, 9)])
    self.assertEqual(result["frame_count"], 8)
```

增加拼图顺序、FFmpeg 参数和不足 8 帧失败测试。通过替换 `subprocess.run` 捕获四次命令，断言输入依次为 `(f1,f2)`、`(f3,f4)`、`(f5,f6)`、`(f7,f8)`，命令包含 `hstack=inputs=2`；7 帧时断言抛出 `ValueError("反推高清帧不足 8 张")`。

- [ ] **Step 2: 运行新测试并确认 RED**

```bash
python -m unittest tests.test_breakdown.BreakdownTests.test_reverse_mode_extracts_eight_high_resolution_frames_and_pairs_them tests.test_breakdown.BreakdownTests.test_pair_reverse_frames_preserves_time_order tests.test_breakdown.BreakdownTests.test_pair_reverse_frames_rejects_fewer_than_eight -v
```

Expected: FAIL，因为 `_pair_reverse_frames` 和反推专用参数尚不存在。

- [ ] **Step 3: 实现可配置抽帧分辨率**

将函数签名改为：

```python
def _extract_frames(video_path, count=6, duration=30, scale_width=512):
    scale_width = max(256, min(int(scale_width or 512), 2048))
```

将场景检测和均匀采样中的 `scale=512:-1` 改为 `scale=%d:-1`，并分别用 `% scale_width` 构造滤镜字符串。

- [ ] **Step 4: 实现严格的双帧拼图**

```python
def _pair_reverse_frames(frame_dir, frames):
    ordered = list(frames or [])
    if len(ordered) < 8:
        raise ValueError("反推高清帧不足 8 张")
    paired = []
    for index in range(4):
        left, right = ordered[index * 2:index * 2 + 2]
        output = os.path.join(frame_dir, "reverse_pair_%d.jpg" % (index + 1))
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", left, "-i", right, "-filter_complex", "hstack=inputs=2",
             "-q:v", "2", output],
            check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        paired.append(output)
    return paired
```

- [ ] **Step 5: 仅在反推路径启用 8 帧和拼图**

在 `_do_breakdown` 中根据 `mode` 选择参数：

```python
is_reverse = mode == _BREAKDOWN_MODE_REVERSE_PROMPT
frame_count = 8 if is_reverse else max(4, min(10, int(duration / 5)))
frame_dir, frames = _extract_frames(
    tmp_video.name, frame_count, duration,
    scale_width=1024 if is_reverse else 512,
)
model_frames = _pair_reverse_frames(frame_dir, frames) if is_reverse else frames
```

反推调用使用 `model_frames`，返回结果的 `frame_count` 仍记录原始 `len(frames)`，缩略图仍来自原始帧。

- [ ] **Step 6: 运行新测试和现有抽帧测试**

```bash
python -m unittest tests.test_breakdown.BreakdownTests.test_reverse_mode_extracts_eight_high_resolution_frames_and_pairs_them tests.test_breakdown.BreakdownTests.test_pair_reverse_frames_preserves_time_order tests.test_breakdown.BreakdownTests.test_pair_reverse_frames_rejects_fewer_than_eight tests.test_breakdown.BreakdownTests.test_extract_frames_clamps_count_to_range -v
```

Expected: 4 tests PASS。

- [ ] **Step 7: 提交 Task 1**

```bash
git add server/content_domains/breakdown.py tests/test_breakdown.py
git commit -m "feat: add reverse prompt timeline frames"
```

### Task 2: 1800 tokens 与六层最低细节数

**Files:**
- Modify: `tests/test_breakdown.py`
- Modify: `server/content_domains/breakdown.py:216-236,430-485`

**Interfaces:**
- Consumes: `_chat_multimodal(sysmsg, usermsg, image_paths, temp=0.7, max_tokens=None, image_detail="low") -> str`。
- Produces: 反推调用传 `max_tokens=1800, image_detail=None`；普通分镜调用维持默认请求体。

- [ ] **Step 1: 写入失败测试**

扩展反推提示词测试，断言源码包含 `主体至少 5 项`、`场景至少 5 项`、`动作与时序至少 8 项`、`镜头至少 5 项`、`光线与色调至少 4 项`、`节奏与情绪钩子至少 3 项` 和双帧“左侧早于右侧”规则。

新增请求体测试：替换 `_post_zhipu` 捕获 body，调用 `_chat_multimodal(..., max_tokens=1800, image_detail=None)`，断言 `body["max_tokens"] == 1800` 且图片对象没有 `detail`；再调用默认参数，断言请求体没有 `max_tokens` 且图片对象的 `detail == "low"`。

- [ ] **Step 2: 运行新测试并确认 RED**

```bash
python -m unittest tests.test_breakdown.BreakdownTests.test_reverse_prompt_requires_minimum_detail_counts tests.test_breakdown.BreakdownTests.test_chat_multimodal_supports_reverse_output_and_image_options -v
```

Expected: FAIL，因为函数尚不接受 `max_tokens` 和 `image_detail`，提示词也没有最低数量。

- [ ] **Step 3: 扩展共享多模态调用的可选参数**

```python
def _chat_multimodal(sysmsg, usermsg, image_paths, temp=0.7,
                     max_tokens=None, image_detail="low"):
```

构造图片对象时，仅在 `image_detail is not None` 时添加 `detail`；构造 body 后，仅在 `max_tokens is not None` 时添加：

```python
body["max_tokens"] = int(max_tokens)
```

- [ ] **Step 4: 加强反推调用与提示词**

反推调用改为：

```python
raw = _chat_multimodal(
    sysmsg, usermsg, frames, temp=0.6,
    max_tokens=1800, image_detail=None,
)
```

在六层指令中加入已批准的最低数量，并写明“双帧图均按左侧早于右侧、图片顺序代表时间推进”。保留 500–800 字和单次调用。

- [ ] **Step 5: 更新受影响的测试替身并运行定向测试**

所有替换 `_chat_multimodal` 且会走反推路径的测试函数增加 `**kwargs`，并在单次调用测试中断言捕获的 `max_tokens == 1800`、`image_detail is None`。

```bash
python -m unittest tests.test_breakdown.BreakdownTests.test_reverse_prompt_requires_minimum_detail_counts tests.test_breakdown.BreakdownTests.test_chat_multimodal_supports_reverse_output_and_image_options tests.test_breakdown.BreakdownTests.test_breakdown_reverse_prompt_calls_model_once -v
```

Expected: 3 tests PASS。

- [ ] **Step 6: 运行完整回归并提交**

```bash
python -m unittest tests.test_breakdown -v
git diff --check
git add server/content_domains/breakdown.py tests/test_breakdown.py
git commit -m "feat: increase reverse prompt detail depth"
```

Expected: 全部测试 PASS；格式检查无输出。

### Task 3: 仓库要求与最终验证

**Files:**
- Verify: `server/content_domains/breakdown.py`
- Verify: `tests/test_breakdown.py`

**Interfaces:**
- Consumes: Task 1–2 的完整实现。
- Produces: 可供审核和 PR 提交的单组、干净分支。

- [ ] **Step 1: 运行资产戳脚本**

```bash
python scripts/stamp_assets.py
```

Expected: 成功且不产生前端文件变化。

- [ ] **Step 2: 运行最终回归和语法检查**

```bash
python -m unittest tests.test_breakdown -q
python -m py_compile server/content_domains/breakdown.py
```

Expected: 全部测试 PASS，Python 编译成功。

- [ ] **Step 3: 检查最终范围**

```bash
git diff --check origin/main...HEAD
git diff --name-only origin/main...HEAD
git status --short
```

Expected: 仅包含本功能的规格/计划、A 组 `breakdown.py` 和关联测试，工作区无未提交修改。
