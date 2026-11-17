import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Protocol, TypeVar

from pydantic import BaseModel, Field

from utils.llm import criar_bedrock_llm


ImpactType = Literal["POSITIVE", "NEGATIVE", "NEUTRAL", "INCONCLUSIVE"]
ModelT = TypeVar("ModelT", bound=BaseModel)


class StructuredLLM(Protocol):
    def with_structured_output(self, schema: type[BaseModel]) -> Any: ...


class CorrelationResult(BaseModel):
    environment_changed: bool
    correlation_detected: bool
    impact_type: ImpactType
    confidence: float = Field(ge=0, le=1)
    summary: str
    evidences: list[str] = Field(default_factory=list)


def criar_llm() -> StructuredLLM:
    return criar_bedrock_llm()


def invocar_estruturado(
    system_prompt: str,
    payload: dict[str, Any],
    schema: type[ModelT],
    llm: Optional[StructuredLLM] = None,
) -> ModelT:
    structured = (llm or criar_llm()).with_structured_output(schema)
    result = structured.invoke([
        ("system", system_prompt),
        ("human", json.dumps(payload, ensure_ascii=False, default=str)),
    ])
    if result is None:
        raise RuntimeError("A LLM não retornou resposta estruturada.")
    if isinstance(result, schema):
        return result
    return schema.model_validate(result)


def resultado_inconclusivo(frente: str, motivo: str) -> CorrelationResult:
    return CorrelationResult(
        environment_changed=False,
        correlation_detected=False,
        impact_type="INCONCLUSIVE",
        confidence=0,
        summary=f"Triagem de {frente} inconclusiva: {motivo}",
        evidences=[],
    )


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def implementation_time(gmud: dict[str, Any]) -> Optional[datetime]:
    for field in (
        "implementation_start",
        "implementation_time",
        "planned_start",
        "start_date",
    ):
        parsed = parse_datetime(gmud.get(field))
        if parsed:
            return parsed
    return None


def entidades_extraidas(state: dict[str, Any]) -> list[str]:
    resultado = state.get("resultado_extracao", {})
    return [
        str(item.get("nome_original") or item.get("nome"))
        for item in resultado.get("entidades", [])
        if item.get("nome_original") or item.get("nome")
    ]
