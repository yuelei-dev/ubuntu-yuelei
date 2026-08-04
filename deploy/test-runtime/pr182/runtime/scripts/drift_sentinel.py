#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄雀主站「漂移哨兵」——检测有人绕过 ship 直接热改服务器文件。

巡检不再依赖上次 bless 的基线，而是直接比较：
  线上运行文件 == 服务器 checkout 中的 git origin/main 文件

这样即使有人错误执行过 bless，只要线上与 git 不一致仍会报警。

用法：
  drift_sentinel.py                         巡检(cron 用)，有漂移则飞书告警(带冷却)
  drift_sentinel.py --print                 只打印漂移，不告警
  drift_sentinel.py --bless                 兼容旧调用：记录当前 origin/main 应有清单
  drift_sentinel.py --bless-deploy file...  记录本次 ship 部署的文件清单，不改变巡检判断
                                            （可选 --pr <号> 或 env HQ_DEPLOY_PR 记录关联 PR）
  drift_sentinel.py --verify-deploy file... 校验本次 ship 文件线上 == git origin/main
  drift_sentinel.py --test                  发一条飞书自检消息

已登记例外（仅巡检路径生效，--verify-deploy 一律绕开、严格比对）：
  例外条目 = path + drift_kind + 指纹（expected: sha256 运行时内容哈希 / ref 等于某
  不可变提交（完整 40 位 SHA，拒绝分支名/tag/短 SHA）版本 / absent 仅用于 missing）。drift_kind 与指纹完全匹配才进 registered 桶单独汇总、
  不计入漂移数；命中但状态已变（kind/指纹不符）按真漂移处理并标注「登记例外状态已变」。
  漂移桶之外还会独立遍历清单全部条目核对当前实际状态：恢复基线内容、missing 被补上、
  added 被删除都算偏离登记 → exceptions_stale；stale-only（无普通漂移）同样返回非零并告警。
  清单来源顺序：env HQ_DRIFT_EXCEPTIONS（本地 JSON）→
  git show <HQ_DRIFT_EXCEPTIONS_REF 或 GIT_REF>:deploy/test-server-exceptions.json；
  读不到按无例外处理（行为同旧版）；schema 非法则告警并按零例外巡检，绝不崩溃。

告警渠道复用 openclaw 的飞书（~/.openclaw/openclaw.json）+ balance_alert 的告警群。
零依赖（仅标准库）。
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

HOME = os.path.expanduser('~')
DRIFT_DIR = os.path.join(HOME, 'hq-drift')
BASELINE = os.path.join(DRIFT_DIR, 'baseline.json')
DEPLOY_LOG = os.path.join(DRIFT_DIR, 'deploy_bless.jsonl')
STATE = os.path.join(DRIFT_DIR, '.state.json')
LOG = os.path.join(DRIFT_DIR, 'sentinel.log')
COOLDOWN = 6 * 3600

WEBROOT = os.environ.get('HQ_WEBROOT', '/var/www/huangquechuanmei')
REPO = os.environ.get('HQ_REPO', os.path.join(HOME, 'huangque-main-site'))
GIT_REF = os.environ.get('HQ_DRIFT_REF', 'origin/main')
EXCEPTIONS_GIT_PATH = 'deploy/test-server-exceptions.json'

BACKEND_RUNTIME = {
    'server/auth_server.py': '/home/ubuntu/auth-service/auth_server.py',
    'server/content_api.py': '/home/ubuntu/content-api/content_api.py',
    'server/imggen_api.py': '/home/ubuntu/content-api/imggen_api.py',
    'server/leadgen_api.py': '/home/ubuntu/content-api/leadgen_api.py',
    'server/tikhub.py': '/home/ubuntu/content-api/tikhub.py',
    # func_names：content 和 admin 【两个服务】都 import 它。漏部署 = 两个服务一起起不来。
    'server/func_names.py': '/home/ubuntu/content-api/func_names.py',
    'server/dl_service.py': '/home/ubuntu/dl-service/dl_service.py',
    'server/admin_api.py': '/home/ubuntu/content-api/admin_api.py',
    'server/pricing_config.py': '/home/ubuntu/content-api/pricing_config.py',
    'scripts/drift_sentinel.py': '/home/ubuntu/hq-drift/drift_sentinel.py',
}

CONTENT_DOMAINS_RUNTIME = os.environ.get('HQ_CONTENT_DOMAINS_RUNTIME', '/home/ubuntu/content-api/content_domains')
SYSTEMD_DIR = os.environ.get('HQ_SYSTEMD_DIR', '/etc/systemd/system')


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg
    try:
        os.makedirs(DRIFT_DIR, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line)


def norm_bytes(data):
    return data.replace(b'\r\n', b'\n')


def md5_bytes(data):
    return hashlib.md5(norm_bytes(data)).hexdigest()


def md5_file(path):
    try:
        with open(path, 'rb') as f:
            return md5_bytes(f.read())
    except Exception:
        return None


def git(args, check=True):
    return subprocess.run(
        ['git', '-C', REPO] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def ensure_repo_ref():
    if not os.path.isdir(os.path.join(REPO, '.git')):
        raise RuntimeError('git checkout 不存在：%s' % REPO)
    git(['fetch', '--quiet', 'origin'], check=False)
    git(['rev-parse', '--verify', GIT_REF], check=True)


def git_ls_tree(prefix):
    p = git(['ls-tree', '-r', '--name-only', GIT_REF, prefix], check=False)
    if p.returncode != 0:
        return []
    return [x.strip() for x in p.stdout.decode('utf-8', 'ignore').splitlines() if x.strip()]


def git_md5(git_path):
    p = git(['show', '%s:%s' % (GIT_REF, git_path)], check=False)
    if p.returncode != 0:
        return None
    return md5_bytes(p.stdout)


def git_ref_bytes(ref, git_path):
    p = git(['show', '%s:%s' % (ref, git_path)], check=False)
    return p.stdout if p.returncode == 0 else None


def _active_overlays_path():
    return os.path.join(DRIFT_DIR, 'active_overlays.json')


def _overlay_expectations_from_record(record):
    """Return {normal git path: immutable deployed postimage metadata}."""
    if not isinstance(record, dict):
        raise ValueError('overlay record 必须是对象')
    ref = record.get('deploy_sha')
    manifest_path = record.get('manifest')
    if not isinstance(ref, str) or not re.fullmatch(r'[0-9a-f]{40}', ref):
        raise ValueError('overlay deploy_sha 必须是完整 40 位 SHA')
    if not isinstance(manifest_path, str) or not re.fullmatch(r'deploy/test-runtime/[^/]+/manifest\.json', manifest_path):
        raise ValueError('overlay manifest 路径非法')
    raw = git_ref_bytes(ref, manifest_path)
    if raw is None:
        raise ValueError('不可变 overlay manifest 不可读取: %s:%s' % (ref, manifest_path))
    manifest = json.loads(raw.decode('utf-8'))
    if manifest.get('schema_version') != 1 or not str(manifest.get('target', '')).startswith('test:'):
        raise ValueError('overlay manifest schema/target 非法')
    overrides = manifest.get('overrides', [])
    if (not isinstance(overrides, list)
            or any(not isinstance(item, str)
                   or not re.fullmatch(r'deploy/test-runtime/[^/]+/manifest\.json', item)
                   for item in overrides)
            or len(overrides) != len(set(overrides))
            or manifest_path in overrides):
        raise ValueError('overlay overrides 非法')
    out = {}
    for entry in manifest.get('deploy_files', []):
        if not isinstance(entry, dict):
            raise ValueError('overlay deploy_files 条目非法')
        source, target = entry.get('source'), entry.get('target')
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError('overlay source/target 非法')
        allowed = ('/home/ubuntu/', '/var/www/huangquechuanmei/', '/etc/systemd/system/')
        if (not target.startswith(allowed) or target.endswith('/')
                or not re.fullmatch(r'[A-Za-z0-9._/-]+', target)):
            raise ValueError('overlay target 越界: %s' % target)
        runtime_git_path = runtime_to_git_path(target)
        if not runtime_git_path:
            raise ValueError('overlay target 无法映射到巡检路径: %s' % target)
        postimage = git_ref_bytes(ref, source)
        if postimage is None:
            raise ValueError('overlay postimage 不可读取: %s:%s' % (ref, source))
        if hashlib.sha256(postimage).hexdigest() != entry.get('sha256'):
            raise ValueError('overlay postimage sha256 与 manifest 不一致: %s' % source)
        if runtime_git_path in out:
            raise ValueError('overlay runtime path 重复: %s' % runtime_git_path)
        out[runtime_git_path] = {
            'runtime': target,
            'source': source,
            'deploy_sha': ref,
            'md5': md5_bytes(postimage),
            'manifest': manifest_path,
            'overrides': list(overrides),
        }
    if not out:
        raise ValueError('overlay manifest 没有部署文件')
    return out


def _merge_active_overlay_expectations(records):
    out = {}
    for record in records:
        for git_path, item in _overlay_expectations_from_record(record).items():
            previous = out.get(git_path)
            if previous and previous['manifest'] not in item['overrides']:
                raise ValueError('活动 overlay 目标冲突且未显式覆盖: %s' % git_path)
            out[git_path] = item
    return out


def load_active_overlay_expectations():
    path = _active_overlays_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
        if not isinstance(doc, dict) or doc.get('schema_version') != 1 or not isinstance(doc.get('overlays'), list):
            raise ValueError('active_overlays schema 非法')
        return _merge_active_overlay_expectations(doc['overlays'])
    except Exception as exc:
        # 活动 overlay 已成为该运行路径的权威期望。清单损坏时若退回旧例外，
        # 旧 preimage 可能再次被豁免，形成假阴性；必须让本次巡检非零失败。
        log('活动 overlay 清单无效，巡检 fail-closed: %s' % exc)
        raise ValueError('活动 overlay 清单无效: %s' % exc) from exc


def activate_overlay(manifest_path, deploy_sha, pr=None):
    record = {'manifest': manifest_path.replace('\\', '/'), 'deploy_sha': deploy_sha,
              'activated_at': int(time.time())}
    if pr:
        record['pr'] = str(pr)
    # 写状态前先从不可变提交完整解析 postimage，任何缺项均 fail-closed。
    _overlay_expectations_from_record(record)
    path = _active_overlays_path()
    os.makedirs(DRIFT_DIR, exist_ok=True)
    overlays = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            old = json.load(f)
        if old.get('schema_version') != 1 or not isinstance(old.get('overlays'), list):
            raise ValueError('现有 active_overlays schema 非法，拒绝覆盖')
        overlays = [item for item in old['overlays'] if item.get('manifest') != record['manifest']]
    overlays.append(record)
    # Validate the complete candidate stack before replacing the active state.
    # A later overlay may replace an earlier target only when its immutable
    # manifest explicitly names that earlier manifest in `overrides`.
    _merge_active_overlay_expectations(overlays)
    temp = path + '.tmp.%s' % os.getpid()
    with open(temp, 'w', encoding='utf-8') as f:
        json.dump({'schema_version': 1, 'overlays': overlays}, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    log('活动 overlay 已原子启用：%s @ %s%s' % (
        record['manifest'], deploy_sha, '（PR #%s）' % pr if pr else ''))


EXCEPTION_KINDS = ('changed', 'missing', 'added')
EXPECTED_TYPES = ('sha256', 'ref', 'absent')
EXCEPTION_KEYS = {'path', 'drift_kind', 'expected', 'reason', 'registered'}


def _validate_exceptions(data):
    """严格 schema 校验，返回 {git path: 条目}；任何非法抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError('顶层必须是对象')
    if data.get('schema_version') != 1:
        raise ValueError('schema_version 缺失或不支持（需要 1）')
    items = data.get('exceptions')
    if not isinstance(items, list):
        raise ValueError('exceptions 必须是数组')
    out = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError('第 %d 条不是对象' % i)
        if set(item) != EXCEPTION_KEYS:
            raise ValueError('第 %d 条字段必须恰好为 %s，实际 %s' % (i, sorted(EXCEPTION_KEYS), sorted(item)))
        path, kind, expected = item['path'], item['drift_kind'], item['expected']
        if not isinstance(path, str) or not path.strip():
            raise ValueError('第 %d 条 path 非法' % i)
        path = path.replace('\\', '/')
        if kind not in EXCEPTION_KINDS:
            raise ValueError('%s drift_kind 非法: %r' % (path, kind))
        if not isinstance(item['reason'], str) or not isinstance(item['registered'], str):
            raise ValueError('%s reason/registered 必须是字符串' % path)
        if not isinstance(expected, dict):
            raise ValueError('%s expected 必须是对象' % path)
        etype = expected.get('type')
        if etype not in EXPECTED_TYPES:
            raise ValueError('%s expected.type 非法: %r' % (path, etype))
        if etype == 'absent':
            if set(expected) != {'type'}:
                raise ValueError('%s absent 型 expected 不允许 value 字段' % path)
            if kind != 'missing':
                raise ValueError('%s absent 型只用于 missing 类例外' % path)
        else:
            if set(expected) != {'type', 'value'} or not isinstance(expected.get('value'), str) or not expected['value']:
                raise ValueError('%s expected.value 缺失或非法' % path)
            if kind == 'missing':
                raise ValueError('%s missing 类例外必须用 absent 型' % path)
            if etype == 'sha256' and not re.fullmatch(r'[0-9a-f]{64}', expected['value']):
                raise ValueError('%s sha256 必须是 64 位小写十六进制' % path)
            if etype == 'ref' and not re.fullmatch(r'[0-9a-f]{40}', expected['value']):
                # 必须是完整 40 位不可变提交 SHA——分支名/tag/短 SHA 会随 fetch 移动，
                # 移动 ref 会让「线上 == ref」的判定自动跟随，形成假阴性
                raise ValueError('%s ref 必须是完整 40 位提交 SHA，拒绝分支名/tag/短 SHA: %r' % (path, expected['value']))
        if path in out:
            raise ValueError('重复 path: %s' % path)
        item = dict(item, path=path)
        out[path] = item
    return out


def load_exceptions():
    """读取并校验已登记例外，返回 {git path: 条目}。

    来源顺序：env HQ_DRIFT_EXCEPTIONS（本地 JSON 文件）→
    git show <HQ_DRIFT_EXCEPTIONS_REF 或 GIT_REF>:deploy/test-server-exceptions.json。
    读不到文件 → 空表（静默，行为同旧版）；内容非法 → 告警并按零例外巡检，绝不崩溃。
    """
    src = os.environ.get('HQ_DRIFT_EXCEPTIONS')
    if src:
        try:
            with open(src, 'r', encoding='utf-8') as f:
                raw = f.read()
        except Exception as e:
            log('例外清单配置错误，按零例外巡检（%s 读取失败: %s）' % (src, e))
            return {}
    else:
        ref = os.environ.get('HQ_DRIFT_EXCEPTIONS_REF') or GIT_REF
        p = git(['show', '%s:%s' % (ref, EXCEPTIONS_GIT_PATH)], check=False)
        if p.returncode != 0:
            return {}
        raw = p.stdout.decode('utf-8', 'ignore')
    try:
        return _validate_exceptions(json.loads(raw))
    except Exception as e:
        log('例外清单配置错误，按零例外巡检: %s' % e)
        return {}


def sha256_file(path):
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def _exception_state_matches(item, git_path):
    """不看 drift_kind，只核对当前实际状态（存在性 + 指纹）是否等于登记值。"""
    expected = item['expected']
    etype = expected['type']
    rp = git_path_to_runtime(git_path)
    if etype == 'absent':
        return not (rp and os.path.exists(rp))
    if not rp or not os.path.exists(rp):
        return False
    if etype == 'sha256':
        return sha256_file(rp) == expected['value']
    if etype == 'ref':
        p = git(['show', '%s:%s' % (expected['value'], git_path)], check=False)
        if p.returncode != 0:
            return False
        return md5_file(rp) == md5_bytes(p.stdout)
    return False


def _exception_matches(item, kind, git_path):
    """命中例外的路径，校验 drift_kind 与内容指纹，完全匹配才可豁免。"""
    return item['drift_kind'] == kind and _exception_state_matches(item, git_path)


def _stale_reason(item, drift_kind, git_path):
    """登记例外的当前状态偏离说明。drift_kind=None 表示该路径当前无普通漂移。"""
    rp = git_path_to_runtime(git_path)
    if item['expected']['type'] == 'absent':
        return '登记为 missing（应不存在），当前文件已存在（可能被重新部署）'
    if not (rp and os.path.exists(rp)):
        return '登记为 %s 例外，当前文件不存在（可能被删除或回退）' % item['drift_kind']
    if drift_kind is None:
        return '登记为 %s 例外，当前无漂移但指纹与登记不符（可能已恢复基线内容）' % item['drift_kind']
    return '登记为 %s 例外，当前为 %s 且 drift_kind/指纹与登记不符' % (item['drift_kind'], drift_kind)


def _test_runtime_overlay_target(git_path):
    """Resolve a committed overlay artifact through its sibling manifest.

    Only overlay artifact paths use this indirection. Ordinary source paths keep
    their stable mapping, so an old deployment manifest cannot shadow a future
    normal deployment of the same source file.
    """
    match = re.fullmatch(r'(deploy/test-runtime/[^/]+)/runtime/.+', git_path)
    if not match:
        return None
    manifest_path = match.group(1) + '/manifest.json'
    p = git(['show', '%s:%s' % (GIT_REF, manifest_path)], check=False)
    if p.returncode != 0:
        return None
    try:
        manifest = json.loads(p.stdout.decode('utf-8'))
        if manifest.get('schema_version') != 1 or not str(manifest.get('target', '')).startswith('test:'):
            return None
        matches = [entry for entry in manifest.get('deploy_files', [])
                   if isinstance(entry, dict) and entry.get('source') == git_path]
        if len(matches) != 1:
            return None
        target = matches[0].get('target')
        allowed = ('/home/ubuntu/', '/var/www/huangquechuanmei/', '/etc/systemd/system/')
        if (not isinstance(target, str) or not target.startswith(allowed) or target.endswith('/')
                or not re.fullmatch(r'[A-Za-z0-9._/-]+', target)):
            return None
        if os.path.basename(target) != os.path.basename(git_path):
            return None
        return target
    except (UnicodeDecodeError, ValueError, TypeError):
        return None


def git_path_to_runtime(git_path):
    git_path = git_path.replace('\\', '/')
    overlay_target = _test_runtime_overlay_target(git_path)
    if overlay_target:
        return overlay_target
    if git_path.startswith('site/'):
        return os.path.join(WEBROOT, git_path[len('site/'):])
    if git_path in BACKEND_RUNTIME:
        return BACKEND_RUNTIME[git_path]
    if git_path.startswith('server/content_domains/') and git_path.endswith('.py'):
        return os.path.join(CONTENT_DOMAINS_RUNTIME, os.path.basename(git_path))
    if git_path.startswith('deploy/systemd/'):
        # ship 现在会部署 systemd 单元与 drop-in。没有这段映射，--verify-deploy 会把它们静默跳过，
        # 然后报「N 个文件校验通过」—— 一个虚假的确认，比不校验更糟。
        rel = git_path[len('deploy/systemd/'):]
        if rel.endswith('.example'):
            return None          # 示例文件不落地（doubao.conf 含明文密钥，真值只在服务器上）
        return os.path.join(SYSTEMD_DIR, rel)
    return None


def runtime_to_git_path(path):
    path = os.path.normpath(path)
    webroot = os.path.normpath(WEBROOT)
    if path.startswith(webroot + os.sep):
        rel = os.path.relpath(path, webroot).replace(os.sep, '/')
        if rel.startswith('assets/') or '.bak' in os.path.basename(rel):
            return None
        return 'site/' + rel
    for git_path, runtime in BACKEND_RUNTIME.items():
        if path == os.path.normpath(runtime):
            return git_path
    domain_dir = os.path.normpath(CONTENT_DOMAINS_RUNTIME)
    if path.startswith(domain_dir + os.sep) and path.endswith('.py') and '__pycache__' not in path:
        return 'server/content_domains/' + os.path.basename(path)
    systemd_dir = os.path.normpath(SYSTEMD_DIR)
    if path.startswith(systemd_dir + os.sep):
        return 'deploy/systemd/' + os.path.relpath(path, systemd_dir).replace(os.sep, '/')
    return None


def expected_git_paths():
    paths = []
    for p in git_ls_tree('site'):
        if p.startswith('site/assets/') or '.bak' in os.path.basename(p):
            continue
        paths.append(p)
    for p in BACKEND_RUNTIME:
        if git_md5(p) is not None:
            paths.append(p)
    for p in git_ls_tree('server/content_domains'):
        if p.endswith('.py') and '__pycache__' not in p:
            paths.append(p)
    return sorted(set(paths))


def runtime_files():
    files = []
    for p in glob.glob(os.path.join(WEBROOT, '**', '*'), recursive=True):
        if os.path.isfile(p) and runtime_to_git_path(p):
            files.append(p)
    for p in BACKEND_RUNTIME.values():
        if os.path.isfile(p):
            files.append(p)
    for p in glob.glob(os.path.join(CONTENT_DOMAINS_RUNTIME, '*.py')):
        if os.path.isfile(p) and runtime_to_git_path(p):
            files.append(p)
    return sorted(set(files))


def diff_paths(git_paths=None, apply_exceptions=True):
    """比对线上与 git。apply_exceptions=False 时（--verify-deploy）完全绕开例外机制。"""
    ensure_repo_ref()
    active_overlays = load_active_overlay_expectations() if git_paths is None and apply_exceptions else {}
    wanted = sorted(set(git_paths or expected_git_paths()) | set(active_overlays))
    changed, missing, added, unmapped = [], [], [], []
    expected_runtime = {}
    for gp in wanted:
        active = active_overlays.get(gp)
        rp = active['runtime'] if active else git_path_to_runtime(gp)
        if rp:
            expected_runtime[gp] = rp
            if not os.path.exists(rp):
                missing.append(gp)
                continue
            expected_md5 = active['md5'] if active else git_md5(gp)
            if md5_file(rp) != expected_md5:
                changed.append(gp)
        elif git_paths is not None:
            unmapped.append(gp)

    if git_paths is None:
        expected_git = set(expected_runtime)
        for rp in runtime_files():
            gp = runtime_to_git_path(rp)
            if gp and gp not in expected_git and git_md5(gp) is None:
                added.append(gp)

    result = {
        'changed': sorted(changed),
        'missing': sorted(missing),
        'added': sorted(set(added)),
        'unmapped': sorted(unmapped),
        'active_overlays': sorted(active_overlays),
        'registered': [],
        'exceptions_stale': [],
        'exceptions_stale_reasons': {},
    }
    if apply_exceptions:
        exceptions = load_exceptions()
        classified = set()
        for kind in EXCEPTION_KINDS:
            kept = []
            for gp in result[kind]:
                if gp in active_overlays:
                    # 不可变 postimage 是该运行路径的新期望；旧 preimage 例外已被原子取代。
                    kept.append(gp)
                    classified.add(gp)
                    continue
                item = exceptions.get(gp)
                if item is None:
                    kept.append(gp)
                elif _exception_matches(item, kind, gp):
                    result['registered'].append(gp)
                else:
                    # 命中例外但 drift_kind/指纹不符：登记状态已变，按真漂移处理
                    result['exceptions_stale'].append(gp)
                    result['exceptions_stale_reasons'][gp] = _stale_reason(item, kind, gp)
                    kept.append(gp)
                classified.add(gp)
            result[kind] = kept
        # 独立遍历未进漂移桶的例外：恢复基线内容 / missing 被补上 / added 被删除
        # 都会让路径从漂移集合消失，必须在这里核出，否则登记保留的功能被回退会静默漏报
        for gp, item in exceptions.items():
            if gp in classified or gp in active_overlays:
                continue
            if _exception_state_matches(item, gp):
                result['registered'].append(gp)
            else:
                result['exceptions_stale'].append(gp)
                result['exceptions_stale_reasons'][gp] = _stale_reason(item, None, gp)
        result['registered'].sort()
        result['exceptions_stale'].sort()
    return result


def snapshot():
    ensure_repo_ref()
    return {
        gp: {
            'runtime': git_path_to_runtime(gp),
            'git_md5': git_md5(gp),
            'runtime_md5': md5_file(git_path_to_runtime(gp)) if git_path_to_runtime(gp) else None,
        }
        for gp in expected_git_paths()
    }


def bless(paths=None, pr=None):
    os.makedirs(DRIFT_DIR, exist_ok=True)
    paths = [p.replace('\\', '/') for p in (paths or [])]
    if paths:
        d = diff_paths(paths)
        record = {
            'deployed_at': int(time.time()),
            'git_ref': GIT_REF,
            'repo': REPO,
            'files': paths,
            'verify': d,
        }
        if pr:
            record['pr'] = str(pr)
        with open(DEPLOY_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        log('部署 bless 已记录：%d 个文件%s' % (len(paths), '（PR #%s）' % pr if pr else ''))
        return
    snap = snapshot()
    with open(BASELINE, 'w', encoding='utf-8') as f:
        json.dump({'blessed_at': int(time.time()), 'git_ref': GIT_REF, 'files': snap}, f, ensure_ascii=False, indent=0)
    log('git 基线清单已记录：%d 个文件（巡检仍以 %s 为准）' % (len(snap), GIT_REF))


def short(git_path):
    return git_path


def _feishu_send(text):
    try:
        fe = json.load(open(os.path.join(HOME, '.openclaw', 'openclaw.json')))['channels']['feishu']
        aid, sec = fe['appId'], fe['appSecret']
    except Exception as e:
        log('读飞书配置失败: %s' % e); return False
    gid = None
    try:
        for b in json.load(open(os.path.join(HOME, 'agent-metrics', 'bot_groups.json'))):
            for g in b['in_groups']:
                if g['name'] == '父OpenClaw开发测试':
                    gid = g['id']
    except Exception:
        pass
    if not gid:
        log('未找到告警群 chat_id'); return False
    try:
        tok = json.load(urllib.request.urlopen(urllib.request.Request(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            data=json.dumps({'app_id': aid, 'app_secret': sec}).encode(),
            headers={'Content-Type': 'application/json'}), timeout=10)).get('tenant_access_token')
        body = json.dumps({'receive_id': gid, 'msg_type': 'text',
                           'content': json.dumps({'text': text})}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(
            'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
            data=body, headers={'Authorization': 'Bearer ' + tok,
                                'Content-Type': 'application/json'}), timeout=10))
        return r.get('code') == 0
    except Exception as e:
        log('飞书发送失败: %s' % e); return False


def format_diff(d):
    total = len(d['changed']) + len(d['missing']) + len(d['added'])
    registered = d.get('registered') or []
    stale = d.get('exceptions_stale') or []
    reasons = d.get('exceptions_stale_reasons') or {}
    active = d.get('active_overlays') or []
    if total == 0 and stale:
        # stale-only：无普通漂移，但登记例外的状态/指纹已偏离登记值
        lines = ['‼️ 黄雀主站登记例外状态已变 %d 处（线上与 git %s 比对无普通漂移，但例外登记不再成立）' % (len(stale), GIT_REF)]
    else:
        lines = ['⚠️ 黄雀主站检测到 %d 处文件漂移（线上与 git %s 不一致）' % (total, GIT_REF)]
    for tag, key in (('改动', 'changed'), ('删除', 'missing'), ('新增', 'added')):
        if d[key]:
            lines.append('【%s %d】%s' % (tag, len(d[key]), '、'.join(short(p) for p in d[key][:12])))
    if registered:
        lines.append('已登记例外 %d 处（见 %s，指纹匹配，不计入漂移）' % (len(registered), EXCEPTIONS_GIT_PATH))
    if active:
        lines.append('活动部署 overlay %d 个运行路径（不可变 postimage 指纹）' % len(active))
    if stale:
        lines.append('‼️ 登记例外状态已变 %d 处（按真漂移处理）：' % len(stale))
        for p in stale[:12]:
            lines.append('  - %s：%s' % (p, reasons.get(p, '状态与登记不符')))
    lines.append('→ 请勿直接改服务器；正常上线必须走 PR 合并后由审核方执行 ship。')
    return '\n'.join(lines)


def handle_detect(print_only=False):
    d = diff_paths()
    total = len(d['changed']) + len(d['missing']) + len(d['added'])
    n_reg = len(d.get('registered') or [])
    n_active = len(d.get('active_overlays') or [])
    n_stale = len(d.get('exceptions_stale') or [])
    if total == 0 and n_stale == 0:
        notes = []
        if n_reg:
            notes.append('已登记例外 %d 处' % n_reg)
        if n_active:
            notes.append('活动 overlay %d 个运行路径' % n_active)
        log('巡检正常：线上与 git %s 一致，无漂移%s' % (
            GIT_REF, '（%s）' % '，'.join(notes) if notes else ''))
        return 0
    # stale-only 也必须非零并告警：登记保留的功能被回退/删除时不能静默漏报
    msg = format_diff(d)
    log('检测到漂移: changed=%d missing=%d added=%d registered=%d stale=%d' % (
        len(d['changed']), len(d['missing']), len(d['added']), n_reg, n_stale))
    if print_only:
        print(msg)
        return 1
    fp = hashlib.md5(msg.encode('utf-8')).hexdigest()
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if st.get('fp') == fp and time.time() - st.get('ts', 0) < COOLDOWN:
        log('漂移未变且在冷却期内，跳过告警')
        return 1
    ok = _feishu_send(msg)
    os.makedirs(DRIFT_DIR, exist_ok=True)
    json.dump({'fp': fp, 'ts': int(time.time())}, open(STATE, 'w'))
    log('已发飞书告警: %s' % ok)
    return 1


def handle_verify(paths):
    paths = [p.replace('\\', '/') for p in paths]
    # 部署后校验必须严格：逐一比对 运行文件 == git GIT_REF，完全绕开巡检例外机制。
    d = diff_paths(paths, apply_exceptions=False)
    total = len(d['changed']) + len(d['missing']) + len(d['added']) + len(d['unmapped'])
    if total == 0:
        log('部署后校验通过：%d 个文件线上 == git %s' % (len(paths), GIT_REF))
        return 0
    print(format_diff(d))
    if d['unmapped']:
        print('无法映射到运行路径: ' + ', '.join(d['unmapped']))
    log('部署后校验失败：changed=%d missing=%d added=%d unmapped=%d' % (
        len(d['changed']), len(d['missing']), len(d['added']), len(d['unmapped'])))
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true')
    ap.add_argument('--print', dest='print_only', action='store_true')
    ap.add_argument('--bless', action='store_true')
    ap.add_argument('--bless-deploy', nargs='*')
    ap.add_argument('--verify-deploy', nargs='*')
    ap.add_argument('--activate-overlay')
    ap.add_argument('--deploy-sha')
    ap.add_argument('--pr', default=os.environ.get('HQ_DEPLOY_PR'),
                    help='--bless-deploy 记录的关联 PR 号（也可用 env HQ_DEPLOY_PR）')
    args = ap.parse_args()

    if args.test:
        print('飞书自检:', _feishu_send('【漂移哨兵自检】黄雀主站文件漂移监测通道正常，可忽略'))
        return 0
    if args.activate_overlay is not None:
        if not args.deploy_sha:
            ap.error('--activate-overlay 必须同时传 --deploy-sha <40位SHA>')
        try:
            activate_overlay(args.activate_overlay, args.deploy_sha, pr=args.pr)
        except Exception as exc:
            log('活动 overlay 启用失败: %s' % exc)
            return 2
        return 0
    if args.verify_deploy is not None:
        return handle_verify(args.verify_deploy)
    if args.bless_deploy is not None:
        bless(args.bless_deploy, pr=args.pr)
        return 0
    if args.bless:
        bless()
        return 0
    return handle_detect(args.print_only)


if __name__ == '__main__':
    sys.exit(main())
