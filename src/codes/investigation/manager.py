import hashlib
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph_checkpoint_aws import DynamoDBSaver

from agent.correlation_common import StructuredLLM, parse_datetime
from agent.extract_agent import no_extrair_entidades, normalizar_payload_gmud
from agent.gmud_correlation_agent import correlate_degradation_with_gmud
from collector import datadog_collector as dd
from .degradation_analyzer import analyze_trajectory, compare_snapshot
from .health_snapshot import collect_health_snapshots, iso_utc
from .snapshot_summary import sanitize_entity_results, summarize_snapshot
from .windows import matching_reference_window


ANALYSIS_WINDOW_MINUTES = int(os.getenv("INVESTIGATION_WINDOW_MINUTES", "60"))
MAX_ENTITIES = int(os.getenv("INVESTIGATION_MAX_ENTITIES", "0"))
CHECKPOINT_TABLE = os.getenv("LANGGRAPH_CHECKPOINT_TABLE", "")
CHECKPOINT_TTL_SECONDS = int(
    os.getenv("LANGGRAPH_CHECKPOINT_TTL_SECONDS", "2592000")
)


class InvestigationState(TypedDict, total=False):
    gmud_payload: dict[str, Any]
    gmud: dict[str, Any]
    investigation_id: str
    change_number: str | None
    implemented_at: str
    environment: str
    entities: list[dict[str, Any]]
    targets: list[dict[str, Any]]
    baselines: dict[str, dict[str, Any]]
    entity_results: list[dict[str, Any]]
    samples: dict[str, list[dict[str, Any]]]
    last_window_end: str
    window_minutes: int
    window_from: str | None
    window_to: str | None
    degradation: dict[str, Any]
    gmud_correlation: dict[str, Any]
    analyzed_at: str


def _investigation_id(gmud: dict[str, Any]) -> str:
    reference = gmud.get("sys_id") or gmud.get("number")
    if not reference:
        raise ValueError("A GMUD precisa conter number ou sys_id.")
    digest = hashlib.sha256(str(reference).encode()).hexdigest()[:24]
    return f"investigation-{digest}"


def _implemented_at(gmud: dict[str, Any]) -> datetime:
    for key in (
        "implementation_start",
        "implementation_time",
        "work_start",
        "start_date",
    ):
        parsed = parse_datetime(gmud.get(key))
        if parsed:
            return parsed
    raise ValueError("A GMUD não informa o momento de implementação.")


def _target(entity: dict[str, Any], environment: str) -> dict[str, Any] | None:
    name = str(entity.get("nome_original") or entity.get("nome") or "").strip()
    if not name:
        return None
    entity_type = str(entity.get("subtipo") or entity.get("tipo") or "").lower()
    safe = name.replace('"', '\\"')
    safe_environment = str(environment).replace('"', '\\"')
    metric_service_tag = f'service:"{safe}"'
    metric_environment_tag = f'env:"{safe_environment}"'
    mappings = {
        "ec2_instance": (
            f'host:"{safe}" env:"{safe_environment}"',
            f'host:"{safe}",{metric_environment_tag}',
        ),
        "kafka_topic": (
            f'@topic:"{safe}" env:"{safe_environment}"',
            f'topic:"{safe}",{metric_environment_tag}',
        ),
        "sqs_queue": (
            f'@queue_name:"{safe}" env:"{safe_environment}"',
            f'queue_name:"{safe}",{metric_environment_tag}',
        ),
        "aws_account": (
            f'@aws.account.id:"{safe}" env:"{safe_environment}"',
            f'aws_account:"{safe}",{metric_environment_tag}',
        ),
    }
    query, metric_filter = mappings.get(
        entity_type,
        (
            f'service:"{safe}" env:"{safe_environment}"',
            f"{metric_service_tag},{metric_environment_tag}",
        ),
    )
    key = hashlib.sha256(f"{entity_type}|{name}".encode()).hexdigest()[:16]
    metric_name = "".join(
        character.lower() if character.isalnum() else "_"
        for character in name
    ).strip("_") or "entity"
    metric_prefix = metric_name
    return {
        "key": key,
        "name": name,
        "type": entity.get("tipo"),
        "subtype": entity.get("subtipo"),
        "telemetry_query": query,
        "metric_filter": metric_filter,
        "metric_names": [
            f"{metric_prefix}.latency_ms",
            f"{metric_prefix}.error_rate",
            f"{metric_prefix}.request",
            f"{metric_prefix}.error",
            f"{metric_prefix}.change_marker",
        ],
    }


def _service_name_variants(service: str) -> list[str]:
    name = service.strip()
    if not name:
        return []
    return list(dict.fromkeys((
        name.lower().replace(" ", "-"),
        name.lower().replace(" ", ""),
    )))


def _add_dependent_service_targets(
    targets: list[dict[str, Any]],
    environment: str,
) -> list[dict[str, Any]]:
    service_types = {"sistema", "servico", "sigla_sistema"}
    expanded_targets = list(targets)
    seen_service_names = {target["name"].casefold() for target in targets}

    for target in targets:
        entity_type = str(target.get("subtype") or target.get("type") or "").lower()
        if entity_type not in service_types:
            continue
        dependencies = dd.get_service_dependencies(target["name"], environment)
        called_by = dependencies.get("called_by")
        if not isinstance(called_by, list):
            raise RuntimeError(
                f"Resposta inválida de dependências para o serviço "
                f"{target['name']!r}: 'called_by' ausente ou inválido."
            )
        for dependent_service in called_by:
            if (
                not isinstance(dependent_service, str)
                or not dependent_service.strip()
            ):
                raise RuntimeError(
                    f"Resposta inválida de dependências para o serviço "
                    f"{target['name']!r}: nome de dependente inválido."
                )
            if dependent_service.casefold() in seen_service_names:
                continue
            dependent_target = _target(
                {
                    "nome": dependent_service,
                    "nome_original": dependent_service,
                    "tipo": "servico",
                },
                environment,
            )
            if dependent_target is None:
                continue
            seen_service_names.add(dependent_service.casefold())
            expanded_targets.append(dependent_target)

    return expanded_targets


def _add_extracted_name_variants(
    extracted_targets: list[dict[str, Any]],
    environment: str,
    extracted_names: set[str],
) -> list[dict[str, Any]]:
    service_types = {"sistema", "servico", "sigla_sistema"}
    expanded_targets = list(extracted_targets)
    seen_names = {target["name"].casefold() for target in extracted_targets}

    for target in extracted_targets:
        if target["name"].casefold() not in extracted_names:
            continue
        entity_type = str(target.get("subtype") or target.get("type") or "").lower()
        if entity_type not in service_types:
            continue
        for name in _service_name_variants(target["name"]):
            if name.casefold() in seen_names:
                continue
            variant_target = _target(
                {
                    "nome": name,
                    "nome_original": name,
                    "tipo": target.get("type"),
                    "subtipo": target.get("subtype"),
                },
                environment,
            )
            if variant_target is None:
                continue
            seen_names.add(name.casefold())
            expanded_targets.append(variant_target)

    return expanded_targets


def _prepare(state: InvestigationState) -> dict[str, Any]:
    payload = state.get("gmud_payload")
    if not payload:
        raise ValueError("Informe um gmud_payload válido.")
    gmud = normalizar_payload_gmud(payload)
    implemented = _implemented_at(gmud)
    environment_value = gmud.get("u_ambient")
    environment = str(
        payload.get("nom_rfrc_ambi_serv")
        or gmud.get("environment")
        or (
            environment_value.get("value")
            if isinstance(environment_value, dict)
            else environment_value
        )
        or "production"
    )
    existing_targets = state.get("targets")
    if existing_targets and state.get("entities"):
        return {
            "gmud": gmud,
            "investigation_id": _investigation_id(gmud),
            "change_number": gmud.get("number"),
            "implemented_at": iso_utc(implemented),
            "environment": environment,
            "baselines": {},
            "entity_results": sanitize_entity_results(
                state.get("entity_results", [])
            ),
        }
    extraction = no_extrair_entidades({"gmud": payload})["resultado_extracao"]
    entities = extraction.get("entidades", [])
    candidates = list(entities)
    if gmud.get("service"):
        candidates.insert(0, {
            "nome": gmud["service"],
            "nome_original": gmud["service"],
            "tipo": "servico",
            "subtipo": None,
        })
    extracted_targets = []
    seen = set()
    for entity in candidates:
        target = _target(entity, environment)
        if target and target["key"] not in seen:
            seen.add(target["key"])
            extracted_targets.append(target)

    targets = _add_extracted_name_variants(
        extracted_targets,
        environment,
        {
            str(entity.get("nome_original") or entity.get("nome") or "")
            .strip()
            .casefold()
            for entity in entities
        },
    )
    targets = _add_dependent_service_targets(targets, environment)
    if MAX_ENTITIES > 0:
        targets = targets[:MAX_ENTITIES]
    if not targets:
        raise ValueError("Nenhuma entidade consultável foi extraída da GMUD.")
    return {
        "gmud": gmud,
        "investigation_id": _investigation_id(gmud),
        "change_number": gmud.get("number"),
        "implemented_at": iso_utc(implemented),
        "environment": environment,
        "entities": entities,
        "targets": targets,
        "baselines": {},
        "entity_results": sanitize_entity_results(
            state.get("entity_results", [])
        ),
    }


def _collect_and_compare(state: InvestigationState) -> dict[str, Any]:
    implemented = parse_datetime(state["implemented_at"])
    if implemented is None:
        raise ValueError("Data de implementação inválida no state.")
    end = parse_datetime(state.get("window_to")) or datetime.now(timezone.utc)
    explicit_start = parse_datetime(state.get("window_from"))
    previous_end = parse_datetime(state.get("last_window_end"))
    start = explicit_start or previous_end or max(
        implemented,
        end - timedelta(minutes=int(state.get("window_minutes", ANALYSIS_WINDOW_MINUTES))),
    )
    if start >= end:
        raise ValueError("A janela atual precisa terminar depois da janela anterior.")

    baseline_start, baseline_end = matching_reference_window(
        implemented, start, end
    )
    baselines = collect_health_snapshots(
        state["targets"],
        state["environment"],
        baseline_start,
        baseline_end,
    )
    snapshots = collect_health_snapshots(
        state["targets"],
        state["environment"],
        start,
        end,
    )
    samples = {key: list(value) for key, value in state.get("samples", {}).items()}
    results = []
    for target in state["targets"]:
        key = target["key"]
        baseline = baselines[key]
        snapshot = snapshots[key]
        comparison = compare_snapshot(baseline, snapshot)
        history = samples.setdefault(key, [])
        history.append({
            "step": len(history) + 1,
            "sampled_at": snapshot["sampled_at"],
            "comparison": comparison,
        })
        results.append({
            "entity": target,
            "baseline_summary": summarize_snapshot(baseline),
            "snapshot": snapshot,
            "comparison": comparison,
            "trajectory": analyze_trajectory(history),
        })
    return {
        "baselines": {},
        "entity_results": results,
        "samples": samples,
        "last_window_end": iso_utc(end),
    }


def _correlate(state: InvestigationState) -> dict[str, Any]:
    degraded = [
        result for result in state["entity_results"]
        if result["comparison"].get("degraded")
    ]
    comparison = {
        "degraded": bool(degraded),
        "entities_analyzed": len(state["entity_results"]),
        "degraded_entities": [item["entity"]["name"] for item in degraded],
        "entity_comparisons": [
            {"entity": item["entity"], "comparison": item["comparison"]}
            for item in state["entity_results"]
        ],
    }
    snapshot = {
        "window": state["entity_results"][0]["snapshot"]["window"],
        "metrics": {
            item["entity"]["name"]: item["snapshot"].get("metrics", {})
            for item in state["entity_results"]
        },
        "evidence": {
            "logs": [
                {"entity": item["entity"]["name"], **evidence}
                for item in state["entity_results"]
                for evidence in item["snapshot"].get("evidence", {}).get("logs", [])
            ],
            "apm": [
                {"entity": item["entity"]["name"], **evidence}
                for item in state["entity_results"]
                for evidence in item["snapshot"].get("evidence", {}).get("apm", [])
            ],
        },
    }
    correlation = correlate_degradation_with_gmud(
        state["gmud"], state["entities"], comparison, snapshot
    ).model_dump()
    trajectories = [item["trajectory"] for item in state["entity_results"]]
    return {
        "degradation": {
            "degradation_detected": bool(degraded),
            "degraded_entities": comparison["degraded_entities"],
            "persistent_entities": [
                item["entity"]["name"]
                for item in state["entity_results"]
                if item["trajectory"].get("persistent")
            ],
            "entity_trajectories": trajectories,
        },
        "gmud_correlation": correlation,
        "analyzed_at": iso_utc(datetime.now(timezone.utc)),
    }


@lru_cache(maxsize=1)
def _graph():
    if not CHECKPOINT_TABLE:
        raise RuntimeError("Defina LANGGRAPH_CHECKPOINT_TABLE.")
    checkpointer = DynamoDBSaver(
        table_name=CHECKPOINT_TABLE,
        ttl_seconds=CHECKPOINT_TTL_SECONDS,
        enable_checkpoint_compression=True,
    )
    builder = StateGraph(InvestigationState)
    builder.add_node("prepare", _prepare)
    builder.add_node("collect_and_compare", _collect_and_compare)
    builder.add_node("correlate", _correlate)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "collect_and_compare")
    builder.add_edge("collect_and_compare", "correlate")
    builder.add_edge("correlate", END)
    return builder.compile(checkpointer=checkpointer)


def run_investigation(
    gmud_payload: dict[str, Any],
    window_minutes: int = ANALYSIS_WINDOW_MINUTES,
    window_from: Optional[str] = None,
    window_to: Optional[str] = None,
    llm: Optional[StructuredLLM] = None,
) -> dict[str, Any]:
    del llm  # O grafo cria as LLMs nos nós para permitir checkpoint/resume.
    gmud = normalizar_payload_gmud(gmud_payload)
    thread_id = _investigation_id(gmud)
    result = _graph().invoke(
        {
            "gmud_payload": gmud_payload,
            "window_minutes": window_minutes,
            "window_from": window_from,
            "window_to": window_to,
        },
        {"configurable": {"thread_id": thread_id}},
    )
    return {
        key: result.get(key)
        for key in (
            "investigation_id", "change_number", "environment", "entities",
            "implemented_at", "analyzed_at", "entity_results", "degradation",
            "gmud_correlation", "last_window_end",
        )
    }
