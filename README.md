# 职业技能标准互认服务

本项目用于建设面向业务人员的纯服务端系统。代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入应通过可替换端口接入，以便稳定复现状态变化。运行数据与本地配置不得写入源码目录。

## 功能

- **标准版本管理**：导入标准版本（能力单元 + 证据要求），草稿 → 发布 → 退役的生命周期；版本可分叉（同一父版本的多个后继）。
- **双向映射与规则比对**：对两个能力单元按规则计算双向结论——完全互认（full）、附条件互认（conditional）、不可互认（none）；双向互认取较差方向。规则阈值集中在 `domain.py`（学时比 80%、等级差 1 级、范围覆盖 60%、缺证据 2 项），规则版本号随决定持久化。
- **专家例外**：专家可对特定版本的映射提出带期限（`valid_from`/`valid_until`，必须带时区偏移）的例外；例外需多方会签（两国主管方 + 登记方）后生效，生效期内覆盖规则结论，过期或撤销后自动回落。
- **源标准更新**：发布/退役新版本时，涉及旧版本的活跃映射被标记为 `affected`；历史决定只追加不改写，`recompute` 以新序号追加决定。
- **追溯**：映射的全部历史决定、例外与会签记录、审计事件按时间线输出。
- **审批约束**：会签要求多方权限（签署方必须持有对应 party），并按 `(例外, 幂等键)`、`(例外, 签署方)` 双唯一约束保证幂等重放。

## 结构

```
service_09252_002/
  ports.py     可替换端口：时钟 / ID 生成器（测试注入 FixedClock、SequentialIds）
  domain.py    领域模型与互认规则（纯函数）
  storage.py   SQLite 结构定义与仓储（历史表只增不改）
  services.py  应用服务：导入、比对、会签、撤销、追溯（权限与幂等在此强制）
  api.py       接口边界：stdlib http.server 的 JSON REST 适配器
  app.py       组合根：装配端口与服务
  __main__.py  服务入口
tests/         unittest 测试（循环映射、版本分叉、跨时区、重启一致性、API 端到端）
```

## 运行

```bash
python3 -m service_09252_002
```

环境变量：`SKILL_MUTUAL_HOST`（默认 127.0.0.1）、`SKILL_MUTUAL_PORT`（默认 8080）、
`SKILL_MUTUAL_DB`（默认 `~/.local/share/service_09252_002/mutual.db`，不在源码目录）、
`SKILL_MUTUAL_ADMIN_TOKEN`（引导管理员令牌，开发默认 `dev-admin-token`）。

## API 摘要

除 `GET /health` 外均需 `Authorization: Bearer <令牌>`。错误统一为
`{"error": {"code", "message"}}`，状态码：400/401/403/404/409。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/actors` | 创建参与者（admin）；角色 admin/expert/authority/registry |
| POST | `/standards/import` | 导入标准版本（幂等：同内容返回既有，异内容 409） |
| GET | `/standards/{id}` | 标准及其版本列表 |
| GET | `/versions/{id}` | 版本及能力单元、证据要求 |
| POST | `/versions/{id}/publish` | 发布版本，标记受影响映射 |
| POST | `/versions/{id}/retire` | 退役版本，标记受影响映射 |
| GET | `/versions/{id}/affected-mappings` | 该版本涉及的受影响映射 |
| POST | `/compare` | 即席双向比对（不落库） |
| POST | `/mappings` | 建立双向映射并计算结论（幂等） |
| GET | `/mappings/{id}` | 映射当前视图（叠加生效中的例外） |
| POST | `/mappings/{id}/recompute` | 重新计算：追加新决定，状态回到 active |
| POST | `/mappings/{id}/revoke` | 撤销映射（登记方/管理员） |
| GET | `/mappings/{id}/trace` | 追溯：决定史 + 例外会签 + 审计事件 |
| GET | `/units/{id}/recognition-path?to={id}` | 互认链查找（循环安全） |
| POST | `/exceptions` | 专家提出带期限例外（expert） |
| GET | `/exceptions/{id}` | 例外状态（含派生的生效状态） |
| POST | `/exceptions/{id}/approvals` | 会签：`{party, decision, idempotency_key}` |
| POST | `/exceptions/{id}/revoke` | 撤销例外（提案方撤回未决；登记方撤销已批准） |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

```bash
python3 -m compileall -q service_09252_002 tests
```
