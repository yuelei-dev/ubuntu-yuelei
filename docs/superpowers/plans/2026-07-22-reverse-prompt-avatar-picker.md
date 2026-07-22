# Reverse Prompt Avatar Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional single-avatar picker and 5/10/15-second duration selector before generating video from a reverse-prompt result.

**Architecture:** Keep the existing reverse-prompt result and job polling UI. A new picker in `script.html` returns `{avatarId, duration}`; no avatar submits the existing `script_to_video` drama payload, while an avatar submits the existing `cinematic` open-mode payload. Both paths reuse one generalized job submission/polling function, and all billing, ownership checks, refunds, queues, and assets remain server-owned by their existing endpoints.

**Tech Stack:** Static HTML/CSS, browser JavaScript, existing JSON job APIs, Python `unittest` source-contract tests, Node.js syntax validation.

## Global Constraints

- The picker allows zero or one avatar; multiple avatar selection is out of scope.
- Durations are exactly `5`, `10`, or `15` seconds; default is `10`.
- Displayed video costs are exactly `150`, `300`, and `450` points, derived as `duration * 30`.
- Default selection is “不使用形象” plus 10 seconds.
- With an avatar, submit `/api/gen/cinematic` using `cine_mode: "open"`, `ratio: "9:16"`, `resolution: "720p"`, and `enhance_prompt: false`.
- Without an avatar, submit `/api/gen/script_to_video` with `style: "剧情"` and the selected duration.
- Reverse-prompt pricing remains 20 points per link and is separate from video pricing.
- Do not modify database schema, provider keys, engine implementations, video pricing, or the existing video-page cinematic panel.
- Do not add avatar creation, reference media, prompt enhancement, or multi-avatar behavior to this picker.

---

### Task 1: Optional avatar and duration picker

**Files:**
- Modify: `site/workbench/script.html:1408-1422` (modal markup)
- Modify: `site/workbench/script.html:1156-1182` (picker functions)
- Modify: `tests/test_script_actions_ui.py` (source-contract tests)

**Interfaces:**
- Consumes: `reversePromptText()`, `tok()`, `esc()`, and `GET /api/gen/video/avatars?limit=60`.
- Produces: `_showReverseVideoPicker(prompt, onConfirm)` where `onConfirm` receives `{avatarId: number|null, duration: 5|10|15}`.

- [ ] **Step 1: Add failing picker markup and behavior tests**

Add these tests to `ScriptActionsUiTests`:

```python
def test_reverse_video_picker_has_optional_avatar_duration_and_cost(self):
    self.assertIn('id="reverseVideoPickModal"', self.html)
    self.assertIn('id="reverseVideoNoAvatar"', self.html)
    self.assertIn('id="reverseVideoAvatarGrid"', self.html)
    self.assertIn('data-reverse-duration="5"', self.html)
    self.assertIn('data-reverse-duration="10"', self.html)
    self.assertIn('data-reverse-duration="15"', self.html)
    self.assertIn('id="reverseVideoCost"', self.html)
    self.assertIn("selectedDuration=10", self.html)
    self.assertIn("selectedAvatarId=null", self.html)
    self.assertIn("selectedDuration*30", self.html)

def test_reverse_video_picker_load_failure_keeps_no_avatar_available(self):
    self.assertIn("function _showReverseVideoPicker(prompt,onConfirm)", self.html)
    self.assertIn("fetch('/api/gen/video/avatars?limit=60'", self.html)
    self.assertIn("形象加载失败，不影响无形象生成", self.html)
    self.assertIn("还没有形象", self.html)
    self.assertIn("video.html", self.html)

def test_reverse_video_picker_cancel_and_submit_are_explicit(self):
    self.assertIn('id="reverseVideoPickClose"', self.html)
    self.assertIn('id="reverseVideoConfirm"', self.html)
    self.assertIn("confirm.disabled=true", self.html)
    self.assertIn("onConfirm({avatarId:selectedAvatarId,duration:selectedDuration})", self.html)
```

- [ ] **Step 2: Run the picker tests and verify RED**

Run:

```bash
python -m unittest tests.test_script_actions_ui.ScriptActionsUiTests.test_reverse_video_picker_has_optional_avatar_duration_and_cost tests.test_script_actions_ui.ScriptActionsUiTests.test_reverse_video_picker_load_failure_keeps_no_avatar_available tests.test_script_actions_ui.ScriptActionsUiTests.test_reverse_video_picker_cancel_and_submit_are_explicit -v
```

Expected: all three tests fail because the new modal and `_showReverseVideoPicker` do not exist.

- [ ] **Step 3: Add the modal markup**

Add a sibling modal after `avatarPickModal` in `script.html`. Use these stable IDs and data attributes:

```html
<div id="reverseVideoPickModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:9999;align-items:center;justify-content:center;">
  <div style="background:#111827;border:1px solid rgba(148,164,187,.2);border-radius:18px;padding:24px;width:620px;max-width:calc(100vw - 28px);max-height:86vh;overflow:auto;">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
      <div><b style="font-size:16px;">生成视频</b><div style="font-size:12px;color:#94a4bb;margin-top:4px;">形象可选；不选择时生成纯剧情视频</div></div>
      <button id="reverseVideoPickClose" type="button">&times;</button>
    </div>
    <button id="reverseVideoNoAvatar" type="button">不使用形象</button>
    <div id="reverseVideoAvatarGrid"></div>
    <a href="video.html">去创建形象</a>
    <div id="reverseVideoDuration">
      <button type="button" data-reverse-duration="5">5 秒 · 150 点</button>
      <button type="button" data-reverse-duration="10">10 秒 · 300 点</button>
      <button type="button" data-reverse-duration="15">15 秒 · 450 点</button>
    </div>
    <div id="reverseVideoCost">预计消耗 300 点</div>
    <button id="reverseVideoConfirm" type="button">确认生成</button>
  </div>
</div>
```

Match the page’s existing dark/light-compatible colors and button styles; keep the IDs and visible copy exact so tests and user guidance remain stable.

- [ ] **Step 4: Implement selection state and avatar loading**

Add `_showReverseVideoPicker` near `_showAvatarPicker`. Its state and submission boundary must follow this shape:

```javascript
function _showReverseVideoPicker(prompt,onConfirm){
  var modal=document.getElementById('reverseVideoPickModal');
  var grid=document.getElementById('reverseVideoAvatarGrid');
  var noAvatar=document.getElementById('reverseVideoNoAvatar');
  var confirm=document.getElementById('reverseVideoConfirm');
  var cost=document.getElementById('reverseVideoCost');
  var selectedAvatarId=null, selectedDuration=10;
  var avatars=[];

  function sync(){
    noAvatar.classList.toggle('on',selectedAvatarId===null);
    grid.querySelectorAll('[data-reverse-avatar]').forEach(function(card){
      card.classList.toggle('on',String(card.dataset.reverseAvatar)===String(selectedAvatarId));
    });
    modal.querySelectorAll('[data-reverse-duration]').forEach(function(btn){
      btn.classList.toggle('on',Number(btn.dataset.reverseDuration)===selectedDuration);
    });
    cost.textContent='预计消耗 '+(selectedDuration*30)+' 点';
  }
  function close(){ modal.style.display='none'; }

  noAvatar.onclick=function(){ selectedAvatarId=null; sync(); };
  modal.querySelectorAll('[data-reverse-duration]').forEach(function(btn){
    btn.onclick=function(){ selectedDuration=Number(btn.dataset.reverseDuration); sync(); };
  });
  document.getElementById('reverseVideoPickClose').onclick=close;
  confirm.disabled=false;
  confirm.onclick=function(){
    confirm.disabled=true;
    close();
    onConfirm({avatarId:selectedAvatarId,duration:selectedDuration});
  };
  modal.onclick=function(e){ if(e.target===modal) close(); };
  modal.style.display='flex';
  grid.innerHTML='<div>正在读取形象…</div>';
  sync();

  fetch('/api/gen/video/avatars?limit=60',{headers:{'Authorization':'Bearer '+tok()}})
    .then(function(r){ return r.ok?r.json():Promise.reject(new Error('形象加载失败')); })
    .then(function(d){
      avatars=(d&&d.items)||[];
      grid.innerHTML=avatars.length?'':'<div>还没有形象，可继续无形象生成</div>';
      avatars.forEach(function(a){
        var card=document.createElement('button');
        card.type='button';
        card.dataset.reverseAvatar=a.id;
        card.innerHTML=(a.image_url?'<img src="'+esc(a.image_url)+'" alt="">':'')+'<span>'+esc(a.name||('形象 '+a.id))+'</span>';
        card.onclick=function(){ selectedAvatarId=a.id; sync(); };
        grid.appendChild(card);
      });
      sync();
    })
    .catch(function(){ grid.innerHTML='<div>形象加载失败，不影响无形象生成</div>'; });
}
```

Do not auto-select the first avatar. Closing by the X or backdrop must not call `onConfirm` and must not submit or deduct points.

- [ ] **Step 5: Run picker tests and syntax validation**

Run:

```bash
python -m unittest tests.test_script_actions_ui -v
node -e "const fs=require('fs'),vm=require('vm');const s=fs.readFileSync('site/workbench/script.html','utf8');for(const m of s.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)){if(m[1].trim())new vm.Script(m[1]);}"
```

Expected: `tests.test_script_actions_ui` passes and Node exits 0.

- [ ] **Step 6: Commit the picker**

```bash
git add site/workbench/script.html tests/test_script_actions_ui.py
git commit -m "feat: add reverse video avatar picker"
```

---

### Task 2: Route reverse video generation by picker result

**Files:**
- Modify: `site/workbench/script.html:1200-1256` (generalized job submission)
- Modify: `site/workbench/script.html:1278-1308` (reverse-prompt action)
- Modify: `tests/test_script_actions_ui.py` (routing and safety tests)

**Interfaces:**
- Consumes: `_showReverseVideoPicker(prompt, onConfirm)` from Task 1.
- Produces: `_doGenerate(payload, btn, options)` where `options.endpoint` defaults to `/api/gen/script_to_video` and `options.sceneCount` defaults to `payload.scenes.length`.

- [ ] **Step 1: Add failing routing tests**

Add these tests:

```python
def test_reverse_video_without_avatar_uses_drama_and_selected_duration(self):
    self.assertIn("_showReverseVideoPicker(prompt,function(choice)", self.html)
    self.assertIn("dur:choice.duration+'s'", self.html)
    self.assertIn("style:'剧情',duration:choice.duration", self.html)
    self.assertIn("endpoint:'/api/gen/script_to_video'", self.html)

def test_reverse_video_with_avatar_uses_existing_cinematic_api(self):
    self.assertIn("endpoint:'/api/gen/cinematic'", self.html)
    self.assertIn("cine_mode:'open'", self.html)
    self.assertIn("avatar_ids:[choice.avatarId]", self.html)
    self.assertIn("prompt:prompt", self.html)
    self.assertIn("duration:choice.duration", self.html)
    self.assertIn("ratio:'9:16'", self.html)
    self.assertIn("resolution:'720p'", self.html)
    self.assertIn("enhance_prompt:false", self.html)

def test_shared_video_submitter_supports_both_endpoints_safely(self):
    self.assertIn("function _doGenerate(payload,btn,options)", self.html)
    self.assertIn("options=options||{}", self.html)
    self.assertIn("options.endpoint||'/api/gen/script_to_video'", self.html)
    self.assertIn("(payload.scenes||[]).length", self.html)
    self.assertIn("confirm.disabled=true", self.html)
```

Also update the existing `test_breakdown_remake_reuses_current_one_click_flow`: replace its assertion for immediate reverse-mode `_doGenerate(...)` with assertions for `_showReverseVideoPicker(prompt,function(choice)` and the two endpoint options. Keep its non-reverse `_pickRemakeStyle` assertions unchanged.

- [ ] **Step 2: Run routing tests and verify RED**

Run:

```bash
python -m unittest tests.test_script_actions_ui.ScriptActionsUiTests.test_reverse_video_without_avatar_uses_drama_and_selected_duration tests.test_script_actions_ui.ScriptActionsUiTests.test_reverse_video_with_avatar_uses_existing_cinematic_api tests.test_script_actions_ui.ScriptActionsUiTests.test_shared_video_submitter_supports_both_endpoints_safely -v
```

Expected: failures because `_doGenerate` is still hardcoded and the reverse action does not use the picker.

- [ ] **Step 3: Generalize the existing submit-and-poll helper**

Change its signature and initial variables without duplicating the poll loop:

```javascript
function _doGenerate(payload,btn,options){
  options=options||{};
  var endpoint=options.endpoint||'/api/gen/script_to_video';
  var sceneCount=options.sceneCount==null?(payload.scenes||[]).length:options.sceneCount;
  _setGenerateBusy(btn,true);
  // retain the existing status rendering and poll loop
  fetch(endpoint,{method:'POST',headers:{'Authorization':'Bearer '+tok(),'Content-Type':'application/json'},body:JSON.stringify(payload)})
```

Keep the existing 401, 402, missing-job, completion, error, timeout, refresh-points, and scene-restoration branches. The helper must never retry the submission POST; only the existing job-status GET loop may repeat.

- [ ] **Step 4: Route the reverse-prompt action from the picker**

Replace only the current reverse-mode immediate submit branch with:

```javascript
if(isReverse){
  var prompt=reversePromptText();
  if(!prompt){ if(window.HQ&&window.HQ.toast) HQ.toast('请先完成提示词反推'); return; }
  _showReverseVideoPicker(prompt,function(choice){
    if(choice.avatarId){
      _doGenerate({
        cine_mode:'open', avatar_ids:[choice.avatarId], prompt:prompt,
        duration:choice.duration, ratio:'9:16', resolution:'720p', enhance_prompt:false
      },bdRemakeBtn,{endpoint:'/api/gen/cinematic',sceneCount:1});
      return;
    }
    var reverseScenes=[{scene:prompt,line:'',dur:choice.duration+'s'}];
    _doGenerate({scenes:reverseScenes,style:'剧情',duration:choice.duration},
                bdRemakeBtn,{endpoint:'/api/gen/script_to_video',sceneCount:1});
  });
  return;
}
```

Leave non-reverse breakdown remake behavior unchanged.

- [ ] **Step 5: Run routing and full UI regression tests**

Run:

```bash
python -m unittest tests.test_script_actions_ui -v
python -m unittest tests.test_script_to_video tests.test_script_actions_ui -v
node -e "const fs=require('fs'),vm=require('vm');const s=fs.readFileSync('site/workbench/script.html','utf8');for(const m of s.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)){if(m[1].trim())new vm.Script(m[1]);}"
git diff --check
```

Expected: all Python tests pass, Node exits 0, and `git diff --check` reports no output.

- [ ] **Step 6: Commit routing changes**

```bash
git add site/workbench/script.html tests/test_script_actions_ui.py
git commit -m "feat: route reverse video by avatar choice"
```

---

### Task 3: Final review and pull request

**Files:**
- Review: `site/workbench/script.html`
- Review: `tests/test_script_actions_ui.py`
- Review: `docs/superpowers/specs/2026-07-22-reverse-prompt-avatar-picker-design.md`

**Interfaces:**
- Consumes: completed Task 1 and Task 2 commits.
- Produces: a reviewed branch and PR containing only the approved UI feature, tests, spec, and plan.

- [ ] **Step 1: Run the final focused suite from a clean tree**

```bash
python -m unittest tests.test_script_actions_ui tests.test_script_to_video tests.test_cost_of -v
node -e "const fs=require('fs'),vm=require('vm');const s=fs.readFileSync('site/workbench/script.html','utf8');for(const m of s.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)){if(m[1].trim())new vm.Script(m[1]);}"
git diff --check origin/main...HEAD
git status --short --branch
```

Expected: all tests pass, JavaScript parses, diff-check is clean, and no uncommitted files remain.

- [ ] **Step 2: Request independent code review**

Review `origin/main..HEAD` against the approved spec. The reviewer must specifically verify:

- no-avatar and avatar routes use the correct existing endpoint;
- selected duration is identical in displayed cost and submitted payload;
- avatar ownership is still enforced by `/api/gen/cinematic`;
- closing/loading failure cannot submit;
- the submit POST cannot be duplicated by repeated confirmation;
- non-reverse breakdown and normal one-click generation remain unchanged.

Expected: no unresolved Critical or Important findings.

- [ ] **Step 3: Push and open a draft PR**

```bash
git push -u origin feature/reverse-prompt-avatar-picker
gh pr create --draft --base main --head feature/reverse-prompt-avatar-picker \
  --title "feat: add avatar choice to reverse prompt video" \
  --body-file C:/Users/23329/reverse-prompt-avatar-picker-pr.md
```

Create `C:/Users/23329/reverse-prompt-avatar-picker-pr.md` as a temporary file before this command. Its body must summarize the two endpoint routes, the 5/10/15-second pricing display, unchanged reverse-prompt pricing, and exact test results; delete it immediately after `gh pr create` succeeds.

- [ ] **Step 4: Merge only after required checks pass**

```bash
gh pr checks --watch
gh pr ready
gh pr merge --merge --delete-branch
```

Expected: required CI is green and the PR reports `MERGED` before any deployment work begins.
