# artcreate

标准化 AI 美术资产生产线 —— 为小团队/独立开发者提供游戏美术资产（场景、立绘、卡面、纹理等）的批量化、可复现、带质量门禁的生成管线。

## 定位

- **API 驱动**：不依赖本地 GPU，生成源走云端 API（火山方舟 Seedream 等，provider 可热插拔）
- **全流程**：结构化输入 → Prompt 编译 → 生成 → 后处理 → 质量门禁 → 资产入库（manifest 溯源）
- **分线生产**：按资产类型划分产线（场景线 / 角色线 / 卡面线 / 纹理线），各线有独立的知识文件与门禁标准
- **可复现**：资产契约 + manifest 记录完整生成参数，任何成品可溯源、可再生

> 架构参照 OpenMontage 四层结构（流水线定义 / 工具注册 / 知识文件 / 质量门禁），组织形态参照 sprite-foundry（资产契约 + 三层评审），角色线知识参照 ai-game-spritesheets 锚点管线。前期调研详见内部决策日志。

## 当前状态

`[x]` 前期调研（同类开源项目、架构对齐）
`[ ]` M1：仓库骨架 + 通道验证
`[ ]` M2：单线最小闭环（场景线：spec → 编译 → 生成 → 后处理 → 入库）
`[ ]` M3：质量门禁 + 资产注册表
`[ ]` M4：角色线（参考图工作流）

## 快速开始（占位）

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 填入你的 API 密钥
```

## 密钥安全

所有密钥通过环境变量 / `.env` 注入，**仓库不接收任何真实密钥**。`.env` 已被 `.gitignore` 排除。
