# 短缺替代料审批

排产前发现关键零件短缺时，计划员提出替代料方案。本项目在领域契约之上实现完整 Python 后端：
登记短缺事件、候选替代、适用车型矩阵、三类分别维护的验证证据与多方审批法定人数，
**先试算替代后需求与竞争风险，全部闸口通过才允许发布**；撤回签署、局部适用变更、
库存变化、多个短缺竞争同一替代料时自动重新评估；正式生效时冻结依据；所有写接口都
**逐项返回被接受或拒绝的原因**。

## 领域约束（对应 `domain/contract.json`）

- **角色**：采购计划员、供应商、质量工程师、仓储管理员。
- **状态**：草拟 → 待确认 → 已下达 → 履行中 → 已关闭。
- **不变量**：
  - 替代适用矩阵（局部适用，未覆盖/不可用车型产生残余缺口）；
  - 多方审批法定人数（可配置每角色人数与最小角色数）；
  - 竞争库存分配（多个短缺竞争同一替代料，按 已生效 > 已下达 > 先登记先得 分配）；
  - 生效证据冻结（生效时对需求/分配/证据/矩阵/签署做不可变快照与指纹）。

未完成全部确认的替代**不可能**进入领料：只有"已下达"才能"生效"，发布要求全部闸口通过。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/substitution/`：审批后端
  - `models.py`：领域模型与状态；
  - `reason_codes.py`：稳定的逐项接受/拒绝原因码；
  - `store.py`：线程安全仓储、库存池版本、可选 JSON 快照；
  - `trial.py`：替代后需求试算、竞争分配、法定人数与风险闸口；
  - `service.py`：全部用例与自动重评级联；
  - `api.py`：基于标准库的 HTTP/JSON 接口（零第三方依赖）。
- `tools/check_contract.py`：契约摘要检查；`tools/run_server.py`：启动服务。
- `tests/`：契约、领域服务、HTTP 接口回归测试。

## 关键流程与重评

| 变化 | 行为 |
| --- | --- |
| 撤回签署 | 法定人数立即重算；已下达方案撤回至待确认并释放发布锁 |
| 局部适用矩阵 / 证据变更 | 依据版本前进，旧签署失效（SIGNATURE_STALE），需重签 |
| 库存 / 在途变化 | 库存池版本前进，试算过期（TRIAL_STALE），全员重评 |
| 新短缺竞争同一替代料 | 竞争指纹变化，按优先级重新分配；已下达失败者撤回待确认 |
| 履行中重评失败 | 状态与冻结依据不变，追加履行风险告警，处置前不可结案 |
| 正式生效 | 冻结需求/分配/证据/矩阵/法定人数/签署快照并计算 `basis_hash` |

竞争中方案若要真正退出占位，计划员可"撤销候选"（`/abandon`，退回草拟）；
履行中/已关闭不可撤销。

## HTTP 接口

所有业务命令返回 200，响应体形如：

```json
{
  "accepted": false,
  "items": [
    {"target": "candidate:C1.release", "accepted": false,
     "code": "QUORUM_MISSING", "message": "多方审批法定人数不足", "detail": {}}
  ],
  "data": {}
}
```

一次发布可同时返回多条拒绝原因，便于逐项整改。主要路由：

```
POST /shortages
POST /candidates                      （含 applicability 适用车型矩阵）
POST /candidates/{id}/applicability   局部适用矩阵变更（触发重评）
POST /candidates/{id}/trial           试算替代后需求/竞争分配/风险
POST /candidates/{id}/submit | release | abandon | effectuate | complete
POST /candidates/{id}/sign | withdraw
POST /candidates/{id}/evidence
POST /candidates/{id}/evidence/confirm   三类证据：engineering/supply/customer
POST /candidates/{id}/warnings/resolve
POST /quorum-rules
POST /stock/on-hand | /stock/inbound | /stock/inbound/remove
GET  /candidates[?state=...]  /candidates/{id}  /shortages/{id}  /health
```

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约检查
python3 tools/check_contract.py domain/contract.json

# 启动服务（可选快照持久化）
python3 tools/run_server.py --host 127.0.0.1 --port 8000 [--snapshot data/state.json]
```

仅使用 Python 3.11+ 标准库，无第三方依赖。
