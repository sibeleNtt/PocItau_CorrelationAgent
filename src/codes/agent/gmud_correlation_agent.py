from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .correlation_common import StructuredLLM, invocar_estruturado


CorrelationConclusion = Literal[
    "RELATED",
    "POSSIBLY_RELATED",
    "UNRELATED",
    "INCONCLUSIVE",
    "NOT_EVALUATED",
]


class SignalAssessment(BaseModel):
    signal: Literal["logs", "metrics", "apm"]
    related: Optional[bool] = None
    rationale: str


class GmudCorrelationResult(BaseModel):
    evaluated: bool
    correlation_detected: bool
    conclusion: CorrelationConclusion
    confidence: float = Field(ge=0, le=1)
    summary: str
    evidences: list[str] = Field(default_factory=list, max_length=10)
    signal_assessments: list[SignalAssessment] = Field(default_factory=list)


SYSTEM_PROMPT = """
Você avalia se uma degradação já detectada por regras determinísticas é
tecnicamente compatível com uma GMUD. Compare apenas os dados fornecidos:
descrição, planos e entidades da mudança; deltas das métricas; amostras de logs;
e spans/APM. O mesmo serviço e a proximidade temporal são contexto, não prova de
causalidade. Não invente mensagens, componentes, dependências nem causalidade.
Use RELATED somente com evidência técnica convergente, POSSIBLY_RELATED quando
houver compatibilidade parcial, UNRELATED quando as evidências contradisserem a
mudança e INCONCLUSIVE quando faltarem evidências semânticas. Cite evidências
curtas presentes na entrada e retorne somente o schema solicitado.
"""


GMUD_FIELDS = (
    "number",
    "short_description",
    "description",
    "implementation_plan",
    "risk_impact_analysis",
    "backout_plan",
    "cmdb_ci",
    "service",
    "service_offering",
    "business_service",
    "u_plataform",
)


def _gmud_context(gmud: dict[str, Any]) -> dict[str, Any]:
    return {
        key: gmud[key]
        for key in GMUD_FIELDS
        if gmud.get(key) not in (None, "", [], {})
    }


def not_evaluated() -> GmudCorrelationResult:
    return GmudCorrelationResult(
        evaluated=False,
        correlation_detected=False,
        conclusion="NOT_EVALUATED",
        confidence=0,
        summary="Correlação com a GMUD não executada porque a janela não degradou.",
    )


def correlate_degradation_with_gmud(
    gmud: dict[str, Any],
    entities: list[dict[str, Any]],
    comparison: dict[str, Any],
    snapshot: dict[str, Any],
    llm: Optional[StructuredLLM] = None,
) -> GmudCorrelationResult:
    if not comparison.get("degraded"):
        return not_evaluated()
    evidence = snapshot.get("evidence", {})
    payload = {
        "gmud": _gmud_context(gmud),
        "entities": entities,
        "deterministic_comparison": comparison,
        "telemetry_evidence": {
            "metrics": snapshot.get("metrics", {}),
            "logs": evidence.get("logs", []),
            "apm": evidence.get("apm", []),
        },
        "window": snapshot.get("window"),
    }
    return invocar_estruturado(
        SYSTEM_PROMPT, payload, GmudCorrelationResult, llm
    )
