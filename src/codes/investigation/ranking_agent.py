from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from agent.correlation_common import StructuredLLM, invocar_estruturado, parse_datetime


class RankedChange(BaseModel):
    rank: int = Field(ge=1, le=3)
    change_number: str
    confidence: float = Field(ge=0, le=1)
    rationale: str


class RankingResult(BaseModel):
    system: str
    degradation_detected: bool
    degradation_start: Optional[str] = None
    ranked_changes: list[RankedChange] = Field(default_factory=list, max_length=3)


SYSTEM_PROMPT = """
Você recebe uma degradação estruturada e uma shortlist de GMUDs concorrentes do
mesmo sistema. Ordene no máximo três mudanças por plausibilidade causal, sem
afirmar causalidade absoluta. Considere score determinístico, proximidade
temporal, compatibilidade técnica da descrição, entidades e trajetória pré/pós.
Use apenas change_number existentes na shortlist. Retorne somente o schema.
"""


def _public_gmud(gmud: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in gmud.items()
        if key not in {"gmud_ofensora", "gabarito", "documento_estruturado", "snow_payload"}
    }


def deterministic_score(
    candidate: dict[str, Any],
    degradation: dict[str, Any],
) -> float:
    start = parse_datetime(degradation.get("degradation_start"))
    implemented = parse_datetime(candidate.get("implemented_at"))
    temporal = 0.0
    if start and implemented and implemented <= start:
        minutes = max(0, (start - implemented).total_seconds() / 60)
        temporal = 1 / (1 + minutes / 60)
    description = " ".join(str(candidate.get("gmud", {}).get(key) or "") for key in (
        "title", "description", "service"
    )).lower()
    affected = " ".join(degradation.get("affected_metrics", [])).lower()
    keywords = []
    if "latency" in affected:
        keywords.extend(("latência", "latencia", "timeout", "pool", "roteamento"))
    if "error" in affected:
        keywords.extend(("erro", "falha", "5xx", "exception", "rollback"))
    technical = min(1.0, sum(word in description for word in keywords) / 3) if keywords else 0
    return round(0.6 * temporal + 0.4 * technical, 4)


def rank_candidates(
    system: str,
    degradation: dict[str, Any],
    candidates: list[dict[str, Any]],
    llm: Optional[StructuredLLM] = None,
) -> RankingResult:
    if not degradation.get("degradation_detected"):
        return RankingResult(system=system, degradation_detected=False)
    shortlist = sorted(
        [
            {
                "change_number": item["change_number"],
                "implemented_at": item.get("implemented_at"),
                "deterministic_score": deterministic_score(item, degradation),
                "entities": item.get("entities", []),
                "gmud_correlation": item.get("gmud_correlation"),
                "gmud": _public_gmud(item.get("gmud", {})),
            }
            for item in candidates
        ],
        key=lambda item: item["deterministic_score"],
        reverse=True,
    )[:8]
    if len(shortlist) <= 1:
        ranked = [
            RankedChange(
                rank=1,
                change_number=item["change_number"],
                confidence=item["deterministic_score"],
                rationale="Única GMUD na janela concorrente; score determinístico.",
            )
            for item in shortlist
        ]
        return RankingResult(
            system=system,
            degradation_detected=True,
            degradation_start=degradation.get("degradation_start"),
            ranked_changes=ranked,
        )
    result = invocar_estruturado(
        SYSTEM_PROMPT,
        {"system": system, "degradation": degradation, "candidates": shortlist},
        RankingResult,
        llm,
    )
    allowed = {item["change_number"] for item in shortlist}
    if any(item.change_number not in allowed for item in result.ranked_changes):
        raise ValueError("Ranking retornou change_number fora da shortlist.")
    return result
