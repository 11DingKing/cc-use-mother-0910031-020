"""内存数据仓储：确定性编号与集中状态。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import CustomerRestriction, Evidence, FrozenBasis, ShortageEvent, SubstituteCandidate


@dataclass
class Store:
    events: dict[str, ShortageEvent] = field(default_factory=dict)
    inventory: dict[str, int] = field(default_factory=dict)  # 替代料号 -> 可用库存
    restrictions: list[CustomerRestriction] = field(default_factory=list)
    bases: list[FrozenBasis] = field(default_factory=list)
    counters: dict[str, int] = field(
        default_factory=lambda: {"event": 0, "candidate": 0, "evidence": 0, "basis": 0}
    )

    def next_id(self, kind: str, prefix: str) -> str:
        self.counters[kind] += 1
        return f"{prefix}-{self.counters[kind]:04d}"

    def find_candidate(self, candidate_id: str) -> tuple[ShortageEvent, SubstituteCandidate] | None:
        for event in self.events.values():
            candidate = event.candidate(candidate_id)
            if candidate is not None:
                return event, candidate
        return None

    def find_evidence(self, evidence_id: str) -> tuple[ShortageEvent, SubstituteCandidate, Evidence] | None:
        for event in self.events.values():
            for candidate in event.candidates:
                for evidence in candidate.evidences:
                    if evidence.evidence_id == evidence_id:
                        return event, candidate, evidence
        return None
