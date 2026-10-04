"""短缺替代料审批后端。

模块划分：

- ``models``：领域模型与状态（与 ``domain/contract.json`` 中的角色、状态一致）。
- ``reason_codes``：接口逐项原因码。
- ``store``：线程安全的仓储与可选 JSON 快照持久化。
- ``trial``：替代后需求试算、竞争库存分配与风险闸口。
- ``service``：应用服务，登记/签署/发布/生效/重评等命令。
- ``api``：基于标准库的 HTTP/JSON 接口。
"""
from .service import Outcome, Service
from .store import Store

__all__ = ["Outcome", "Service", "Store"]
