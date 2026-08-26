# Git 对象库误删恢复手册（sandbox safe-delete 事故）

> 首次触发：2026-08-26 17:36（P2-1~P2-4 提交后执行 `git gc --prune=now`）
> 结果：`.git/objects/pack/` 整目录 + 全部 loose 对象消失 → `git log` 报 `fatal: bad object HEAD`
> 恢复：从 Windows 回收站按原路径还原 268 个对象，`git fsck` 全清，历史零丢失

---

## 1. 事故机理（必须理解，否则会二次踩坑）

WorkBuddy 沙箱的 **safe-delete 钩子**会拦截进程的文件删除调用（`os.unlink` / `Path.unlink` / Win32 删除），
把目标**路由到回收站**而不是真正删除。这在保护用户文件时是好事，但对 `git` 是**致命**的：

`git gc` / `git repack` 的正常流程是「写新 pack → 删旧 pack 与 loose 对象」。
钩子介入后：

1. git 认为删除成功，继续走完流程；
2. 旧对象被搬进回收站；
3. **新 pack 的写入或改名环节同样被干扰**（本次表现为先前的 `geometric-repack` 失败：
   `error: Could not read <sha>` / `fatal: Failed to traverse parents of commit <sha>`）；
4. 最终 `.git/objects/pack/` 不复存在、`.git/objects/info/packs` 被清空（1 字节），
   而 `.git/refs` 与 `.git/packed-refs` **仍然完好** → 出现「`git rev-parse HEAD` 能出 sha，
   但 `git log` / `git cat-file` 全部失败」的诡异症状。

**症状识别（三条同时出现即为本事故）**：

| 检查 | 健康 | 本事故 |
|---|---|---|
| `git rev-parse HEAD` | 输出 sha | **输出 sha（正常！）** |
| `git log --oneline -1` | 输出提交 | `fatal: bad object HEAD` |
| `ls .git/objects/pack/` | 有 `.pack`/`.idx` | **目录不存在** |

---

## 2. 预防（已落地，勿撤销）

本仓库已写入 local config，禁止 git 自行触发任何 repack：

```bash
git config gc.auto 0            # 禁用自动 gc（提交/fetch 后不再后台 repack）
git config gc.autoDetach false  # 禁止后台分离执行（避免静默失败）
git config maintenance.auto false
git config fetch.writeCommitGraph false
```

**铁律：在此沙箱内永不手动执行 `git gc` / `git repack` / `git prune`。**
仓库体积问题交由远端（GitHub）侧处理，或在沙箱外的原生终端执行。

> 附带说明：本次执行 `git gc` 的动机是消除 `git reflog` 里指向已剪枝对象的告警噪音
> （`invalid reflog entry`）。**正确做法**是只截断 reflog 文件本身
> （`: > .git/logs/HEAD`），**到此为止，不要再 gc**。

---

## 3. 恢复步骤（按序执行）

### Step 0 —— 先备份工作区（最重要）

对象库损坏时**工作区文件通常完好**。恢复动作有失败风险，先保住源码：

```bash
mkdir -p /e/Workspace/_HexBroker_srcbackup_$(date +%Y%m%d_%H%M)
cp -r hexbroker tests scripts configs deliverables docs conftest.py pyproject.toml \
      /e/Workspace/_HexBroker_srcbackup_*/
```

### Step 1 —— 确认 refs 还在

```bash
cat .git/HEAD              # 应为 ref: refs/heads/main
cat .git/packed-refs       # 应含 <sha> refs/heads/main
git rev-parse HEAD         # 能出 sha → refs 完好，对象可救
```

若 `packed-refs` 与 `refs/heads/` 双双丢失，则跳到 §4 远端重建。

### Step 2 —— 从回收站还原对象

```bash
python scripts/recover_git_objects_from_recycle.py            # dry-run，先看命中数
python scripts/recover_git_objects_from_recycle.py --apply    # 执行还原
```

脚本要点：

- 解析回收站 `$I******` 元数据（Win10 version=2 格式）取**原始完整路径**，
  再把配对的 `$R******` 数据文件按原路径复制回去；
- **只还原 `objects/` `refs/` `info/`**；
- **绝不还原 `*.lock` 与 `gc.pid`** —— 还原锁文件会直接把 git 锁死
  （`Unable to create '.git/index.lock'`）。这是脚本里最关键的一条过滤；
- 目录条目（`$R` 本身是目录，如 `objects\pack`）走 `copytree(dirs_exist_ok=True)`，
  且**先还原目录、后还原文件**，避免父目录缺失。

> 若回收站 SID 目录与脚本内 `RECYCLE` 常量不一致，先 `ls -d /e/'$RECYCLE.BIN'/*` 取实际值再改。

### Step 3 —— 校验

```bash
git fsck --no-dangling     # 必须「无任何输出」
git log --oneline -5       # 历史应完整
git status --short         # 工作区状态应与事故前一致
git count-objects -vH      # garbage 应为 0
python -m pytest tests/ -q # 代码侧回归
```

`git fsck` 有输出即为未恢复干净，回到 Step 2 检查 `[缺数据]` 与 `[失败]` 行。

---

## 4. 兜底方案：远端重建（仅当回收站也没了）

工作区完好时，历史可从远端重建，**代码内容零丢失**（仅提交哈希改变）：

```bash
git remote -v                       # origin https://github.com/cloudhu/HexBroker.git
mv .git .git_broken_$(date +%s)     # 保留现场，不要删
git init && git remote add origin <url>
git fetch origin main
git reset --soft origin/main        # 工作区不动，仅对齐历史
git add -A && git commit            # 把本地未推送的改动重新提交
```

代价：本地未推送的提交被压成一个新提交、哈希变化。故**日常应及时 push**，
把「唯一副本只在本地」的窗口压到最小。

---

## 5. 检查清单（每次动 `.git` 前过一遍）

- [ ] 工作区已备份？
- [ ] 是否即将执行 `gc` / `repack` / `prune`？→ **停手**
- [ ] 本地是否有未推送提交？→ 先 push
- [ ] 操作后是否跑了 `git fsck --no-dangling`？
