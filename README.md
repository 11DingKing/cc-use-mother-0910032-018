# 抽样检验规则版本

本项目维护抽样检验规则版本的领域约定、角色边界与样例数据，并提供可运行的 Python 服务端：质量部门按物料类别、供应商等级和批量区间维护抽样规则版本，仓库收货时由服务端解析唯一生效版本并生成不可变检验计划，解决"规则已更新、仓库仍按旧表执行、事后无法证明抽样依据"的问题。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/sampling_service/`：抽样规则版本与检验计划服务端（纯标准库，无外部依赖）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动 HTTP 服务。
- `tests/`：契约与服务回归测试。

## 核心业务规则

1. **抽样规则时态**：规则版本按 草拟 → 待确认 → 已下达 流转，下达新版本时旧版本同时关闭，
   生效窗口为 `[effective_from, superseded_at)`；收货时刻有且仅有一个生效版本，否则收货被拒绝。
2. **区间重叠检测**：同一版本内相同（物料类别, 供应商等级）的批量区间不得重叠，
   提交与下达时强制校验，保证任意批量最多命中一条规则。
3. **检验计划冻结**：收货时计划冻结规则快照、缺陷分级与样本量计算全过程；
   新规则版本发布后，已生成及已完成的计划不受影响；已关闭计划拒绝一切修改。
4. **人工覆盖审计**：质量工程师可在计划关闭前覆盖样本量（最终裁定），
   每次覆盖记录前后值、操作者、时间与原因。

样本量计算顺序：**基础比例 → 最小/最大约束 → 不超过批量 → 紧急加严（取最严乘数）
→ 豁免批准（批次级 > 类别+等级级 > 类别级）→ 人工覆盖**。

- **紧急加严**：质量工程师发布乘数 > 1 的加严指令，仅作用于生效时点之后新建的计划，可撤销。
- **豁免批准**：采购计划员或质量工程师申请（免检 skip / 减量 reduce），
  质量工程师批准（申请人与批准人不得相同），支持过期时间。
- **批次拆分**：子批次继承父计划的版本与规则快照，父样本量按子批数量比例分配
  （最大余数法，合计等于父样本量），防止拆分批量规避加严区间；父计划以"批次拆分"原因关闭。

## 角色与权限

| 操作 | 允许角色 |
| --- | --- |
| 规则版本创建/提交/下达、加严发布与撤销、豁免审批、人工覆盖 | 质量工程师 |
| 收货登记、批次拆分、开始/完成检验 | 仓储管理员（完成检验也允许质量工程师） |
| 豁免申请 | 采购计划员、质量工程师 |
| 查询与试算 | 全部角色 |

## 启动服务

```bash
python3 tools/run_server.py --host 127.0.0.1 --port 8080 --data data/sampling_store.json
```

变更类请求需携带操作者身份：请求头 `X-Actor-Name` / `X-Actor-Role`，
或 JSON 请求体中的 `"actor"` / `"role"` 字段（中文角色名建议走请求体）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rule-versions` | 创建草拟版本（含规则列表） |
| POST | `/rule-versions/{id}/submit` | 提交（校验区间重叠） |
| POST | `/rule-versions/{id}/release` | 下达（关闭旧版本，生效窗口开始） |
| GET | `/rule-versions`、`/rule-versions/{id}` | 查询版本 |
| POST | `/receipts` | 收货登记：解析唯一版本，生成检验计划 |
| POST | `/preview-plan` | 试算样本量（不持久化） |
| GET | `/plans`、`/plans/{id}` | 查询计划 |
| GET | `/plans/{id}/explanation` | 样本量计算解释（公式、步骤、版本窗口、缺陷分级） |
| GET | `/plans/{id}/audit` | 审计轨迹（事件与每次人工覆盖） |
| POST | `/plans/{id}/start` `/complete` | 计划状态流转，完成后冻结 |
| POST | `/plans/{id}/overrides` | 人工覆盖样本量（需原因） |
| POST | `/batches/{id}/splits` | 批次拆分（子批数量之和须等于原批量） |
| POST | `/tightenings`、`/tightenings/{id}/revoke` | 紧急加严与撤销 |
| POST | `/exemptions`、`/exemptions/{id}/approve` `/reject` | 豁免申请与审批 |

错误响应统一为 `{"error": {"code", "message", "details"}}`，典型错误码：
`RULE_OVERLAP`、`NO_EFFECTIVE_VERSION`、`RULE_NOT_FOUND`、`DUPLICATE_BATCH`、
`PLAN_CLOSED`、`OVERRIDE_OUT_OF_RANGE`、`EXEMPTION_SELF_APPROVAL`、`SPLIT_QUANTITY_MISMATCH`。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
