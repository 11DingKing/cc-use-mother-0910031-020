# 短缺替代料审批

本项目维护短缺替代料审批的领域约定、角色边界与样例数据，并提供完整 Python 后端：登记短缺事件、候选替代、适用车型、验证证据与审批法定人数，试算替代后需求与风险再决定发布；撤回签署、局部适用、库存变化和多个短缺竞争同一替代料时自动重新评估，正式生效需冻结依据，接口逐项返回被接受或拒绝的原因。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/substitute_approval/`：后端核心（模型、试算引擎、应用服务、HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动 HTTP 服务。
- `tests/`：契约完整性与后端行为回归测试。

## 后端设计

- **角色边界**（契约约定）：采购计划员登记事件与候选、维护客户限制、提交与发布；质量工程师维护工程适配与验证证据；供应商维护供应确认量；仓储管理员维护库存与领料登记。越权操作被拒绝并返回 `角色权限` 原因。
- **状态机**：草拟 → 待确认 → 已下达 → 履行中 → 已关闭。已下达事件遇触发变更自动回退待确认并要求重新评估。
- **试算**（`evaluate`）：逐候选检查工程适配、验证证据覆盖、客户限制（阻断项），并计算适用车型覆盖、供应可用量与风险等级（低/中/高）；事件级检查候选存在与审批法定人数。
- **竞争库存分配**：多个待确认短缺竞争同一替代料时，按需求日期 → 登记顺序 → 事件编号确定性分配；已下达事件的冻结分配被保留，被挤占的候选得到 `被更高优先级短缺占用` 原因。
- **重新评估触发器**：撤回签署、局部适用调整、库存变化、供应确认量变化、证据失效、客户限制更新、新竞争者出现，均使事件回退待确认并记录触发原因。
- **生效证据冻结**：发布时快照需求、适用矩阵、证据引用、有效签署、库存与供应确认量、分配结果和试算报告，计算 `basis_hash`；冻结依据不可变，触发变更后旧依据标记 `superseded` 但内容保留。
- **领料放行**：仅已下达且无待重新评估的事件可领料，数量不得超出冻结分配；未经完整确认的替代无法进入领料。

## HTTP 接口

`python3 tools/run_server.py --port 8080` 启动。所有接口返回 `{"accepted": bool, "items": [{item, accepted, blocking, reasons}], "data": {...}}`；资源不存在返回 404，请求格式错误返回 400。

- `POST /events` 登记短缺事件；`GET /events`、`GET /events/{id}` 查询
- `POST /events/{id}/candidates` 登记候选替代（含替代比例、适用车型）
- `POST /candidates/{id}/fit` 工程适配；`/supply` 供应确认量；`/applicability` 适用车型；`/evidence` 登记验证证据
- `POST /evidence/{id}/invalidate` 证据失效
- `POST /inventory` 库存变化；`POST /restrictions` 客户限制维护
- `POST /events/{id}/submit` 提交待确认；`/sign` 签署；`/withdraw` 撤回签署
- `POST /events/{id}/evaluate` 试算；`GET /events/{id}/evaluation` 最近试算报告
- `POST /events/{id}/publish` 发布并冻结依据；`GET /events/{id}/basis` 查询冻结依据
- `POST /events/{id}/issue/authorize` 领料放行检查；`/issue` 登记领料；`/close` 关闭

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
