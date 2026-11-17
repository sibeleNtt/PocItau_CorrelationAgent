import json
import sys
from pathlib import Path
from typing import Any


CODES_DIR = Path(__file__).resolve().parent
if str(CODES_DIR) not in sys.path:
    sys.path.insert(0, str(CODES_DIR))

from investigation.manager import ANALYSIS_WINDOW_MINUTES, run_investigation  # noqa: E402


def _decode(value: Any) -> Any:
    while isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            break
    return value


def _request(event: dict[str, Any]) -> dict[str, Any]:
    payload: Any = _decode(event)
    if isinstance(payload, dict) and "body" in payload:
        payload = _decode(payload["body"])
    if isinstance(payload, dict) and "message" in payload:
        message = _decode(payload["message"])
        options = {key: value for key, value in payload.items() if key != "message"}
        if isinstance(message, dict):
            return {**options, "gmud_payload": message}
        raise ValueError("body.message precisa conter um objeto de GMUD.")
    if not isinstance(payload, dict):
        raise ValueError("O evento da Lambda precisa ser um objeto JSON.")
    return {
        "gmud_payload": payload.get("gmud_payload", payload),
        "window_minutes": payload.get("window_minutes", ANALYSIS_WINDOW_MINUTES),
        "window_from": payload.get("window_from"),
        "window_to": payload.get("window_to"),
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        request = _request(event)
        result = run_investigation(
            gmud_payload=request["gmud_payload"],
            window_minutes=int(
                request.get("window_minutes", ANALYSIS_WINDOW_MINUTES)
            ),
            window_from=request.get("window_from"),
            window_to=request.get("window_to"),
        )
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result, ensure_ascii=False, default=str),
        }
    except Exception as exc:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "error": {"type": type(exc).__name__, "message": str(exc)}
            }, ensure_ascii=False),
        }
