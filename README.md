# 职业技能标准互认服务

面向院校合作场景的纯服务端系统：保存各国职业技能标准的版本、能力单元、
证据要求与双向映射，按规则计算**完全互认 / 附条件互认 / 不可互认**，
支持专家对特定版本提出带期限的例外，并在源标准更新时标记受影响映射——
历史决定只追加、不改写。

## 架构

代码按领域模型、应用服务、持久化与接口边界组织；时间与标识通过可替换
端口注入，便于稳定复现状态变化。

```
service_09252_002/
├── domain.py     # 领域模型与枚举（版本/映射/例外状态、互认结论、角色）
├── rules.py      # 规则引擎（纯函数）：学时比、实操范围覆盖、等级差 → 结论
├── clock.py      # 时钟端口：SystemClock / MutableClock，时间一律归一 UTC
├── storage.py    # SQLite：WAL + 外键 + 显式事务；决定与审计只追加
├── services.py   # 应用服务：导入、发布、映射、会签、撤销、比对、例外、追溯
├── api.py        # Flask 接口边界：认证头解析、幂等键透传、错误翻译
└── __main__.py   # 启动入口
```

## 运行

```bash
pip install -r requirements.txt
RECOGNITION_DB=/var/lib/recognition/recognition.db python3 -m service_09252_002
# 默认监听 127.0.0.1:8000，可用 HOST / PORT 环境变量覆盖
```

运行数据（SQLite 文件）由 `RECOGNITION_DB` 指定，不写入源码目录。

## 测试

```bash
python3 -m pytest tests -q          # 全部测试（40 个）
python3 -m unittest discover -s tests -v   # 冒烟测试（兼容）
```

覆盖：规则引擎各维度组合、循环映射（A→B→C→A 环检测）、版本分叉
（分支互不影响、历史决定不改写）、跨时区生效（+08:00 / -05:00 / UTC
边界）、API 全流程（权限 + 幂等）、SQLite 重启一致性。

## 编译检查

```bash
python3 -m compileall -q service_09252_002 tests
```

## 互认规则（rules.RULE_VERSION = 1.0.0）

对每个映射按**两个方向**分别计算（A 证被 B 认、B 证被 A 认）：

| 维度 | 完全互认 | 附条件互认 | 不可互认 |
| --- | --- | --- | --- |
| 学时比（源/目标） | ≥ 0.9 | 0.6 – 0.9（补足差额学时） | < 0.6 |
| 实操范围 | 完全覆盖目标要求 | 有交集但有缺项（补实操项） | 无交集 |
| 等级差 | 0 级 | 1 级（补充评估） | ≥ 2 级 |

任一维度"不可"则整体不可互认；否则任一"附条件"则整体附条件（条件合并）。
生效中的例外可宽限：`waive_hours` / `waive_scope` / `waive_level` 豁免对应
维度，`force_full` 期限内直接完全互认。

## 多方权限与会签

请求头：`X-Actor`（操作者）、`X-Roles`（逗号分隔角色）、
`Idempotency-Key`（写操作可选）。

| 角色 | 权限 |
| --- | --- |
| `importer` | 导入标准、发布新版本 |
| `mapper` | 建立映射 |
| `expert` | 提出例外 |
| `approver` | 代表机构会签（须为当事双方机构之一） |
| `admin` | 撤销映射 / 例外 |

会签约束：映射与例外都需**双方机构各一票**才生效；同一操作者不得代表
两个机构签署；同一机构重复签署幂等返回已有结果。映射被标记待复核后进入
新一轮（`approval_round`），双方需重新会签方可恢复生效。

## 幂等约束

- 所有写操作接受 `Idempotency-Key`：同键重放返回首次响应
  （`meta.idempotent_replay: true`），键与操作者、端点绑定，跨者使用返 409；
- 会签 / 撤销天然幂等：重复操作返回当前状态并附 `note`；
- 唯一约束（标准代码、版本标签、单元对、例外代码、机构每轮一票）由
  数据库强制，冲突返 409。

## 时间与例外期限

所有时间以 UTC ISO8601 存储与比较；输入可带任意时区偏移
（如 `2026-10-01T00:00:00+08:00`），naive 输入按 UTC 解释。
例外在 `effective_from <= now < effective_until` 且已会签生效时参与判定。

## 源标准更新与历史保护

发布新版本时，沿 `parent_version_id` 祖先链找到受影响映射，标记
`affected_by_update` 并置为 `under_review`（分叉分支互不影响）；
历史 `decisions` 与 `audit_events` 只插入不更新，追溯接口可完整回放。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/standards/import` | 导入标准及首个版本（含单元与证据） |
| POST | `/standards/<code>/versions` | 发布新版本，标记受影响映射 |
| GET | `/standards/<code>/versions` | 版本谱系 |
| GET | `/versions/<id>/units` | 能力单元与证据要求 |
| GET | `/versions/<id>/impact` | 该版本相关映射及受影响标记 |
| POST | `/mappings` | 建立双向映射 |
| POST | `/mappings/<id>/approve` | 机构会签（body: `party`） |
| POST | `/mappings/<id>/revoke` | 撤销（body: `reason`） |
| GET | `/mappings/<id>/trace` | 追溯：决定、会签、例外、审计事件 |
| POST | `/comparisons` | 两版本间全部生效映射的双向比对 |
| POST | `/exceptions` | 专家提出带期限例外 |
| POST | `/exceptions/<id>/approve` | 例外会签 |
| POST | `/exceptions/<id>/revoke` | 撤销例外 |
| GET | `/units/<id>/equivalence-chain` | 等价链遍历与循环检测 |
