import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, TypedDict

from dotenv import load_dotenv
from pydantic import BaseModel, Field


AGENT_DIR = Path(__file__).resolve().parent
CODES_DIR = AGENT_DIR.parent
SRC_DIR = CODES_DIR.parent
PROJECT_DIR = SRC_DIR.parent

if str(CODES_DIR) not in sys.path:
    sys.path.insert(0, str(CODES_DIR))

load_dotenv(PROJECT_DIR / ".env")
load_dotenv(SRC_DIR / ".env")

from utils.prompt_load import carregar_system_prompt
from utils.llm import criar_bedrock_llm


PROMPT_FILE = SRC_DIR / "input" / "prompts" / "entity_extraction.md"
OUTPUT_FILE = SRC_DIR / "outputs" / "agent" / "entity_extraction.json"
class StructuredLLM(Protocol):
    def with_structured_output(self, schema: type[BaseModel]) -> Any: ...


class EstadoExtracao(TypedDict, total=False):
    """Contrato de entrada e saída para orquestradores baseados em estado."""

    gmud: dict[str, Any]
    contexto_gmud: dict[str, Any]
    resultado_extracao: dict[str, Any]


class Entidade(BaseModel):
    nome: str = Field(description="Nome normalizado da entidade identificada.")
    tipo: Literal["sistema", "servico", "sigla_sistema", "infra"] = Field(
        description="Tipo principal da entidade."
    )
    subtipo: Optional[Literal[
        "ec2_instance",
        "kafka_topic",
        "sqs_queue",
        "aws_account",
    ]] = Field(default=None, description="Subtipo quando aplicável.")
    nome_original: str = Field(
        description="Forma original exata da entidade na entrada."
    )
    contexto: str = Field(
        description="Trecho curto que sustenta a extração."
    )
    regra_extracao: Literal[
        "texto_exato",
        "texto_parcial",
        "texto_colchetes",
        "texto_inferido",
        "texto_qualificado",
        "texto_referencia",
    ] = Field(description="Regra usada para extrair a entidade.")
    confianca: float = Field(
        ge=0,
        le=1,
        description="Confiança da extração entre 0 e 1.",
    )


class ResultadoExtracao(BaseModel):
    change_number: Optional[str] = Field(
        default=None,
        description="Identificador da mudança analisada.",
    )
    entidades: list[Entidade] = Field(
        default_factory=list,
        description="Entidades extraídas da mudança.",
    )


def criar_llm() -> StructuredLLM:
    return criar_bedrock_llm()


def carregar_arquivo(caminho: Path) -> dict[str, Any]:
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")
    return json.loads(caminho.read_text(encoding="utf-8"))


def desserializar_detalhes(payload: dict[str, Any]) -> dict[str, Any]:
    """Converte o envelope legado ``memorias.txt_detl_soli_autm``."""
    detalhes_raw = payload.get("memorias", {}).get("txt_detl_soli_autm")
    if not detalhes_raw:
        raise ValueError("Campo 'memorias.txt_detl_soli_autm' não encontrado.")
    if isinstance(detalhes_raw, dict):
        return detalhes_raw
    try:
        detalhes = json.loads(detalhes_raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "O campo txt_detl_soli_autm não contém um JSON válido."
        ) from exc
    if not isinstance(detalhes, dict):
        raise ValueError("Os detalhes desserializados precisam ser um objeto.")
    return detalhes


def normalizar_payload_gmud(payload: dict[str, Any]) -> dict[str, Any]:
    """Aceita envelopes legado, registro da API ou documento persistido."""
    if "memorias" in payload:
        return desserializar_detalhes(payload)
    detalhes_raw = payload.get("txt_detl_soli_autm")
    if detalhes_raw:
        if isinstance(detalhes_raw, dict):
            return detalhes_raw
        try:
            detalhes = json.loads(detalhes_raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "O campo txt_detl_soli_autm não contém um JSON válido."
            ) from exc
        if not isinstance(detalhes, dict):
            raise ValueError("Os detalhes da GMUD precisam ser um objeto.")
        return detalhes
    documento = payload.get("documento_estruturado")
    if isinstance(documento, dict):
        return documento
    return payload


def display_value(valor: Any) -> Any:
    if isinstance(valor, dict):
        return valor.get("displayValue") or valor.get("value")
    return valor


def construir_contexto_gmud(detalhes: dict[str, Any]) -> dict[str, Any]:
    """Seleciona campos semânticos e reduz ruído administrativo da GMUD."""
    campos = {
        "number": detalhes.get("number"),
        "service": detalhes.get("service"),
        "short_description": (
            detalhes.get("short_description") or detalhes.get("title")
        ),
        "description": detalhes.get("description"),
        "implementation_plan": detalhes.get("implementation_plan"),
        "backout_plan": detalhes.get("backout_plan"),
        "test_plan": detalhes.get("test_plan"),
        "risk_impact_analysis": detalhes.get("risk_impact_analysis"),
        "cmdb_ci": display_value(detalhes.get("cmdb_ci")),
        "service_offering": display_value(detalhes.get("service_offering")),
        "business_service": display_value(detalhes.get("business_service")),
        "service_offerings": detalhes.get("u_service_offerings"),
        "platform": display_value(detalhes.get("u_plataform")),
        "environment": (
            display_value(detalhes.get("u_ambient"))
            or detalhes.get("environment")
        ),
        "release_train": display_value(detalhes.get("u_release_train")),
    }
    return {
        chave: valor
        for chave, valor in campos.items()
        if valor not in (None, "", [], {})
    }


def extrair_entidades(
    contexto: dict[str, Any],
    llm: Optional[StructuredLLM] = None,
) -> ResultadoExtracao:
    system_prompt = carregar_system_prompt(PROMPT_FILE)
    structured_llm = (llm or criar_llm()).with_structured_output(
        ResultadoExtracao
    )
    resultado = structured_llm.invoke([
        ("system", system_prompt),
        (
            "human",
            "Extraia as entidades desta GMUD e respeite rigorosamente o "
            "schema de saída:\n\n"
            + json.dumps(contexto, ensure_ascii=False, indent=2),
        ),
    ])
    if resultado is None:
        raise RuntimeError("O modelo não retornou uma resposta estruturada.")
    if not isinstance(resultado, ResultadoExtracao):
        resultado = ResultadoExtracao.model_validate(resultado)
    if resultado.change_number is None:
        numero = contexto.get("number")
        resultado.change_number = str(numero) if numero is not None else None
    return resultado


def processar_gmud(
    payload: dict[str, Any],
    llm: Optional[StructuredLLM] = None,
) -> ResultadoExtracao:
    detalhes = normalizar_payload_gmud(payload)
    contexto = construir_contexto_gmud(detalhes)
    return extrair_entidades(contexto, llm)


def no_extrair_entidades(
    estado: EstadoExtracao,
    llm: Optional[StructuredLLM] = None,
) -> EstadoExtracao:
    """Nó puro: recebe ``gmud`` e devolve contexto e extração estruturada.

    O nó não lê nem grava arquivos. Isso permite reutilização direta em
    LangGraph ou outro orquestrador que combine atualizações parciais de estado.
    """
    gmud = estado.get("gmud")
    if not isinstance(gmud, dict) or not gmud:
        raise ValueError("O estado deve conter uma GMUD no campo 'gmud'.")
    detalhes = normalizar_payload_gmud(gmud)
    contexto = construir_contexto_gmud(detalhes)
    resultado = extrair_entidades(contexto, llm)
    return {
        "contexto_gmud": contexto,
        "resultado_extracao": resultado.model_dump(),
    }


def processar_arquivo(
    caminho: Path,
    llm: Optional[StructuredLLM] = None,
) -> dict[str, Any]:
    payload = carregar_arquivo(caminho)
    itens = payload.get("data") if isinstance(payload.get("data"), list) else None
    if itens is None:
        return processar_gmud(payload, llm).model_dump()
    return {
        "data": [processar_gmud(item, llm).model_dump() for item in itens],
        "meta": {"total": len(itens), "source": str(caminho)},
    }


def salvar_resultado(payload: dict[str, Any]) -> Path:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return OUTPUT_FILE


def _argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extrai entidades de uma GMUD ou lote de GMUDs."
    )
    parser.add_argument("arquivo", type=Path, help="Arquivo JSON de entrada.")
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_FILE,
        help="Arquivo JSON de saída.",
    )
    return parser.parse_args()


def main() -> None:
    args = _argumentos()
    resultado = processar_arquivo(args.arquivo)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    print(f"Output: {args.output.resolve()}")


if __name__ == "__main__":
    main()
