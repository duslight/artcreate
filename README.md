# artcreate

标准化 AI 美术资产生产线 —— 为小团队/独立开发者提供游戏美术资产（场景、立绘、卡面、纹理等）的批量化、可复现、带质量门禁的生成管线。

## 定位

- **API 驱动**：不依赖本地 GPU，生成源走云端 API（火山方舟 Seedream 等，provider 可热插拔）
- **全流程**：结构化输入 → Prompt 编译 → 生成 → 后处理 → 质量门禁 → 资产入库（manifest 溯源）
- **分线生产**：按资产类型划分产线（场景线 / 角色线 / 卡面线 / 纹理线），各线有独立的知识文件与门禁标准
- **可复现**：资产契约 + manifest 记录完整生成参数，任何成品可溯源、可再生
- **经验回写**：拍板自由文本自动蒸馏 → LLM 判定 → 晋升为字典选项（derived/*.yaml，自动 git 留痕）

> 架构参照 OpenMontage 四层结构（流水线定义 / 工具注册 / 知识文件 / 质量门禁），组织形态参照 sprite-foundry（资产契约 + 三层评审），角色线知识参照 ai-game-spritesheets 锚点管线。前期调研详见内部决策日志。

## 当前状态（v1.0.0）

`[x]` 前期调研（同类开源项目、架构对齐）
`[x]` M1：仓库骨架 + 通道验证
`[x]` M2：单线最小闭环（场景线：spec → 编译 → 生成 → 后处理 → 入库）
`[x]` M3：质量门禁 + 资产注册表（机械门禁 / L2 编译自检 / L3 意图回查 / L4 跨批统计）
`[x]` M4：角色线（dHash 锚点一致性管线）
`[x]` 阶段 3.5：分层配置（base → project → derived）+ 字典 schema v2 + spec 校验
`[x]` 阶段 3.6：操作日志系统（events append-only，全动作可溯源）
`[x]` 阶段 5：服务端工作台（任务队列 + 移动端表单）
`[x]` 阶段 6：经验蒸馏 distill + 晋升提案（D22 闭环）
`[x]` 阶段 7-A：角色线锚点 + 调色板量化 + 门禁收紧

## 快速开始

### 本地运行

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 填入你的 API 密钥
python -m artcreate --version
```

**CLI 单链路**（一次完整生成）：

```bash
python -m artcreate run specs/xxx.yaml       # 跑一批（编译→生成→后处理→门禁→入库）
python -m artcreate list --status gated      # 看候选
python -m artcreate select <subject>         # 挑选定稿
python -m artcreate stats                    # L4 跨批统计
```

**本地工作台**（浏览器表单 + 评审视图，默认 127.0.0.1 免令牌）：

```bash
python -m artcreate serve                    # http://127.0.0.1:8870
# 浏览器打开 http://127.0.0.1:8870/workbench.html
```

### 服务器部署

见 `deploy/README.md`（systemd + nginx + 令牌 + 备份完整手册）。要点：

```bash
python -m artcreate serve --host 0.0.0.0 --port 8870   # 配 WORKBENCH_TOKEN + nginx 反代
```

## 主要命令

| 命令 | 用途 |
|---|---|
| `run / compile / lint` | 单链路：完整跑 / 只编译 / 只 L1 检查 |
| `serve` | 评审视图 + 工作台（`--host 0.0.0.0` 供服务器部署） |
| `list / select / reject / gate / diagnose / stats` | 资产评审与诊断 |
| `regenerate` | 从历史 run 再生一批（parent_run 溯源） |
| `distill / proposals / promote / demote` | 经验蒸馏与晋升管理 |
| `anchor / consistency` | 角色线锚点与一致性检查 |

## 配置体系

加载链：`artcreate/base.yaml`（出厂字典）→ `project.yaml`（项目定制）→ `derived/*.yaml`（经验晋升，自动生成）。字典含 sizes / art_styles / asset_types / moods / constraint_axes 五类，选项级 `desc`（人话解释）/ `ui_hidden`（表单隐藏不删）/ `conflict_words`（画风冲突词）。

## 密钥安全

所有密钥通过环境变量 / `.env` 注入，**仓库不接收任何真实密钥**。`.env` 已被 `.gitignore` 排除。服务器部署须配 `WORKBENCH_TOKEN`（访问令牌）。
