"""线程安全的内存仓储，带可选 JSON 快照持久化。

库存以"库存池版本（stock_version）"建模：任何库存/供应变化都会使版本前进，
依赖该库存池的试算结果立即过期（TRIAL_STALE），从而满足
"库存变化时重新评估"。
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from .models import QUALITY, SIGNER_ROLES, SUPPLIER, WAREHOUSE, Candidate, Shortage


@dataclass
class QuorumRule:
    """审批法定人数：每个角色要求的签署人数与最小不同角色数。"""
    rule_id: str
    required_counts: dict[str, int]
    min_roles: int

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "required_counts": dict(self.required_counts), "min_roles": self.min_roles}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QuorumRule":
        return cls(rule_id=raw["rule_id"], required_counts=dict(raw["required_counts"]), min_roles=int(raw["min_roles"]))


@dataclass
class StockPool:
    """替代料库存池：现有库存 + 已确认在途供应。"""
    material: str
    on_hand: float = 0.0
    inbound: dict[str, float] = field(default_factory=dict)  # inbound_id -> qty
    version: int = 0

    def available(self) -> float:
        return self.on_hand + sum(self.inbound.values())

    def to_dict(self) -> dict[str, Any]:
        return {"material": self.material, "on_hand": self.on_hand, "inbound": dict(self.inbound), "version": self.version}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StockPool":
        return cls(
            material=raw["material"],
            on_hand=float(raw.get("on_hand", 0.0)),
            inbound={k: float(v) for k, v in raw.get("inbound", {}).items()},
            version=int(raw.get("version", 0)),
        )


class Store:
    def __init__(self, snapshot_path: Optional[str | Path] = None) -> None:
        self._lock = RLock()
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None
        self.shortages: dict[str, Shortage] = {}
        self.candidates: dict[str, Candidate] = {}
        self.stock: dict[str, StockPool] = {}
        self.quorum_rules: dict[str, QuorumRule] = {}
        # 替代料 -> 最近一次库存版本（用于细粒度感知库存变化）
        self._seen_versions: dict[str, int] = {}
        self._bootstrap_default_quorum()

    # ---- 默认法定人数：质量工程师/供应商/仓储管理员 各至少 1 人，至少 3 个角色 ----
    def _bootstrap_default_quorum(self) -> None:
        self.quorum_rules["standard"] = QuorumRule(
            rule_id="standard",
            required_counts={QUALITY: 1, SUPPLIER: 1, WAREHOUSE: 1},
            min_roles=len(SIGNER_ROLES),
        )

    @property
    def lock(self) -> RLock:
        return self._lock

    # ---- 库存 ----
    def pool(self, material: str) -> StockPool:
        with self._lock:
            pool = self.stock.get(material)
            if pool is None:
                pool = StockPool(material=material)
                self.stock[material] = pool
            return pool

    def touch_stock(self, material: str) -> None:
        """库存发生变化时调用：版本前进。"""
        with self._lock:
            self.pool(material).version += 1

    # ---- 快照持久化 ----
    def save(self) -> None:
        if self.snapshot_path is None:
            return
        with self._lock:
            payload = {
                "shortages": [s.to_dict() for s in self.shortages.values()],
                "candidates": [c.to_dict() for c in self.candidates.values()],
                "stock": [p.to_dict() for p in self.stock.values()],
                "quorum_rules": [r.to_dict() for r in self.quorum_rules.values()],
            }
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.snapshot_path.parent), suffix=".tmp")
            try:
                with open(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                Path(tmp).replace(self.snapshot_path)
            finally:
                p = Path(tmp)
                if p.exists():
                    p.unlink(missing_ok=True)

    def load(self) -> None:
        if self.snapshot_path is None or not self.snapshot_path.exists():
            return
        with self._lock:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            self.shortages = {s["shortage_id"]: Shortage.from_dict(s) for s in payload.get("shortages", [])}
            self.candidates = {c["candidate_id"]: Candidate.from_dict(c) for c in payload.get("candidates", [])}
            self.stock = {p["material"]: StockPool.from_dict(p) for p in payload.get("stock", [])}
            rules = {r["rule_id"]: QuorumRule.from_dict(r) for r in payload.get("quorum_rules", [])}
            if rules:
                self.quorum_rules = rules
