# 抽样检验规则版本

质量部门更新抽样比例与缺陷分级后，仓库仍按旧表执行、事后无法举证某批次为什么只抽了
特定数量——本服务端解决这个问题。它维护**按物料类别、供应商等级与批量区间生效的抽样规则
版本**，在收货时解析出**唯一版本**并生成**不可变检验计划**；紧急加严、豁免批准、规则
重叠、批次拆分均有确定处理；完成的计划不随新规则变化，API 可解释样本量计算过程与每一次
人工覆盖。

## 领域不变量（与 `domain/contract.json` 对齐）

1. **抽样规则时态**：规则以版本为单位整体生效（草拟→待确认→已下达→已关闭）。同一
   (物料类别, 供应商等级) 任意时刻最多一个生效版本；下达新版本自动关闭旧版本，生效窗为
   半开区间 `[issued_at, closed_at)`，收货时刻落在哪个窗就用哪张表。
2. **区间重叠检测**：每个版本的批量区间行必须从 1 起首尾相连、无重叠、无缺口，且只有
   最后一行可为开放区间；创建与下达时都会校验，冲突返回 409 并列出全部问题。
3. **检验计划冻结**：收货时把规则版本 ID、内容指纹（SHA-256）、命中的规则行、缺陷分级
   Ac/Re、字码与样本量完整快照进计划。之后发布新表、关闭版本都不影响已生成/已完成计划。
4. **人工覆盖审计**：紧急加严与豁免均为「仓储申请 → 质量工程师批准」的职责分离流程，
   申请、批准、撤销全部进入只追加审计流；覆盖仅允许在检验开始前操作，开始后样本量固定。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/inspection_service/`：服务端
  - `sampling_tables.py`：GB/T 2828.1 样本量字码表（批量 × 检验水平 → 字码 → 样本量）
  - `models.py`：规则版本、规则行、缺陷分级、不可变检验计划、人工覆盖
  - `store.py`：内存仓储与只追加审计事件流
  - `service.py`：用例编排（时态解析、收货冻结、覆盖审批、批次拆分）
  - `api.py`：标准库 HTTP 接口（无第三方依赖）
  - `seed.py`：演示种子数据
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、领域用例与 HTTP 端到端测试。

## 验证

```bash
# 全部测试（契约 + 领域 + HTTP 端到端，共 30 个）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# 启动带种子数据的演示服务
PYTHONPATH=src python3 -m inspection_service.api --port 8000 --seed
```

## API 概览

请求头：`X-Actor`（操作人）、`X-Role`（角色，支持中文或 `quality_engineer`/`warehouse`
等英文别名）。HTTP 头只允许 latin-1，中文名请按 RFC 3986 percent-encode。
规则写操作与覆盖批准仅**质量工程师**；收货、检验、拆分仅**仓储管理员**。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/rule-versions` | 创建版本（`rows` 批量区间行 + 可选 `defect_classes`） |
| POST | `/api/rule-versions/{id}` | `action=submit/issue/close` |
| GET | `/api/rule-versions[/{id}]` | 版本列表/详情（含指纹、行、缺陷分级） |
| POST | `/api/goods-receipts` | 收货：解析唯一版本并冻结检验计划 |
| GET | `/api/plans/{id}` | 计划详情（快照、样本量、覆盖、轨迹） |
| GET | `/api/plans/{id}/explanation` | **样本量计算解释 + 每次覆盖 + 审计轨迹** |
| POST | `/api/plans/{id}` | `start` / `complete` / `split` / `request-tightening` / `request-exemption` |
| POST | `/api/overrides/{id}` | `action=approve/revoke`（仅质量工程师） |
| GET | `/api/batches?batch_no=` | 批次及其拆分血缘上的全部计划 |
| GET | `/api/audit` | 只追加的全局审计事件流 |

### 关键行为

- **唯一生效解析**：收货时扫描该 (类别, 等级) 的全部版本，过滤
  `status=已下达 且 issued_at ≤ 收货时刻 < closed_at`；解析出 0 个返回 404，
  多于 1 个返回 409。计划内 `resolution_trace` 保留候选版本与选中理由。
- **样本量解释**：`explanation` 给出 批量 → 字码 → 表定样本量 → 规则行基础值 →
  覆盖作用（`ceil(n×倍数)+追加`，豁免降为 0）→ 封顶不超过批量 的完整算式与每次重算历史。
- **紧急加严**：倍数必须 ≥ 1.0（禁止借加严减少抽样），批准前不生效。
- **豁免批准**：批准后样本量为 0（免抽），完成时实抽 0 可正常关闭。
- **批次拆分**：父计划以「批次拆分」关闭冻结，子批量之和必须等于父批量；子计划按拆分
  时刻重新解析规则并独立冻结，父子血缘可经 `/api/batches` 追溯。
- **结果登记**：实抽数量必须等于计划样本量；缺陷按快照分级逐类判定 Ac/Re，分级判不通过
  时禁止直接合格入库，分级明细与总数不一致会被拒绝。

### 快速演练

```bash
# 收货 300 件（种子表 281..500 行，水平 II → 字码 H → 50 件）
curl -s -X POST localhost:8000/api/goods-receipts \
  -H 'Content-Type: application/json' \
  -H 'X-Actor: %E4%BB%93%E5%BA%93%E7%AE%A1%E7%90%86%E5%91%98' -H 'X-Role: warehouse' \
  -d '{"batch_no":"B-1","material_category":"电子元件","supplier_grade":"A",
       "supplier_id":"S-1","lot_qty":300}'

# 查看"为什么抽这么多"
curl localhost:8000/api/plans/IP-00001/explanation
```

## 持久化说明

当前仓储为内存实现（进程重启数据清空），领域层与存储层已解耦：生产环境把
`store.Repository` 换成数据库实现（计划与审计流只追加、不更新）即可，领域规则与 API
无需改动。
