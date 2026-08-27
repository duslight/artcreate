# artcreate

给小团队和独立开发者用的 AI 美术资产工作台。你在浏览器里填一张中文表单，它把表单编译成英文 prompt，调云端 API 出图，跑质量门禁，最后入资产库。生成源是火山方舟 Seedream 4.5（可换 provider），不需要本地 GPU。

一条生成的完整链路：

```
填表（中文） → 编译审核（中英对照，可精修） → 生成 → 后处理（像素化/量化）
→ 门禁（lint / 编译自检 / 意图回查 / 机械检查） → 候选拍板 → 命名发布 exports/final/
```

每一步都有记录：manifest 落盘完整 spec 与编译分段，SQLite 记候选与操作日志。任何成品可溯源、可再生，改稿改的是 spec 不是 prompt。

## 工作台页面

浏览器打开 `http://127.0.0.1:8870/workbench.html`，六个页面：

- **场景 / 静物** —— 场景全景、背景底图、单体物件、卡面、纹理。4 条约束轴（人工痕迹、水体、空间、植被），9 种氛围光影，支持从历史 run 载入迭代。
- **角色生成** —— 立绘冷启动。首张拍板后「设为锚点」，之后该角色的生成都自动挂锚点参考图并跑 dHash 一致性检查。5 条角色轴（取景、背景、朝向、姿势）。三槽参考图：人物管长相，画风管笔触，姿势管构图，各 1 张。
- **怪物生成** —— 非人形生物设计，方法论是剪影优先。7 条怪物轴（背景、体型结构、尺度、威胁定位、标志特征、皮肤材质、威胁基调），独立的三槽参考图库，与角色库互不可见。
- **动作 / 特效** —— 选已有锚点的角色，勾动作（待机/行走/攻击/受击/技能/倒地/起手/跳跃/冲刺），每动作一个 run，批量出动画帧。
- **任务队列 / 历史任务** —— 后台 worker 串行执行，重启可续查；历史带编译词复用。
- **评审视图** —— 全部候选、门禁详情、操作时间线，工作台内嵌切换。

约束轴共 14 条，按页面过滤（`applies_to`）。每档选中即显示注入/排除词的中英对照，还带一条验图问句供 L3 回查。画风当前两种：经典像素（通用），暗黑地牢哥特鸦羽笔手绘（角色/怪物页专属）。要加画风改字典就行，不用动代码。

## 编译器

中文表单进，全英文 prompt 出。注入顺序：

```
资产前缀 → 场景翻译 → 约束轴正向 + 正向自由约束 → LLM 扩展词 → 氛围 → 画风尾块
→ "Strictly avoid: ..." 排除块 → 参考图分工说明句
```

约束先于 LLM 扩展注入。审核卡里确认过的编译词原样执行（审核直通）。

审核卡提供完整 prompt 的中文翻译和逐段来源解释。精修有两种：整段直改（改中文全文，LLM 逐句对齐只重译改动部分），或分段模式（改某段中文，对应英文段同步重译）。

## 门禁

| 层 | 做什么 | 拦什么 |
|---|---|---|
| L1 lint | 纯规则静态扫描 | 「不要X」式否定注入、画风冲突词、token 超限、枚举写错静默失效 |
| L2 编译自检 | LLM 反向核对编译产物 | 翻译丢意、prompt 加了表单没要求的东西 |
| L3 意图回查 | VLM 拿原始约束验成图 | 图不对题（按轴的 verify_question 逐条问） |
| L4 跨批统计 | 统计历史通过率 | 某类约束长期不生效 |
| 机械门禁 | 程序检查像素 | 调色板超色、网格不规则、边缘半透明、尺寸不符（按画风可放宽） |

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 填 ARK_API_KEY 和 ZHIPU_API_KEY
python -m artcreate serve   # http://127.0.0.1:8870/workbench.html
```

生图每张约 0.26 元。数量默认 1，先看效果再加量。

服务器部署见 `deploy/README.md`（systemd、令牌、备份）。公网访问须配 `WORKBENCH_TOKEN`，手机端用 `http://<IP>:8870/workbench.html?token=<令牌>` 直连。

## CLI

网页之外，同一套链路可以脚本化：

```bash
python -m artcreate run specs/xxx.yaml      # 完整跑一批
python -m artcreate compile specs/xxx.yaml  # 只编译看 prompt（不花钱）
python -m artcreate lint specs/xxx.yaml     # 只跑 L1
python -m artcreate list --status gated     # 看候选
python -m artcreate select <subject>        # 拍板定稿
python -m artcreate regenerate <run_id>     # 从历史 run 再生
python -m artcreate stats                   # L4 跨批统计
python -m artcreate anchor set <char> <img> # 手动设锚点
python -m artcreate poses <char> idle,walk  # 动作批量
python -m artcreate distill                 # 蒸馏拍板经验
python -m artcreate proposals               # 看晋升提案 / promote / demote
```

## 密钥

密钥一律走环境变量（`.env`），仓库不接收任何真实密钥。生图走方舟 Seedream 4.5，编译和验图走智谱 glm-4-flash / glm-4v-flash，成本很低。provider 在 project.yaml 里热插拔，也支持自定义的方舟兼容端点。
