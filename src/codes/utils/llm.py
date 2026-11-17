import os

from langchain_aws import ChatBedrockConverse


def criar_bedrock_llm() -> ChatBedrockConverse:
    """Cria a LLM usando o modelo/inference profile configurado no Bedrock."""
    model_id = os.getenv("BEDROCK_MODEL_ID")
    region_name = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    bearer_token = os.getenv("AWS_BEARER_TOKEN_BEDROCK")
    access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
    secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    session_token = os.getenv("AWS_SESSION_TOKEN")

    ausentes = []
    if not model_id:
        ausentes.append("BEDROCK_MODEL_ID")
    if not region_name:
        ausentes.append("AWS_REGION (ou AWS_DEFAULT_REGION)")
    if ausentes:
        raise RuntimeError(
            "Variáveis obrigatórias não definidas: " + ", ".join(ausentes)
        )

    credenciais = {}
    if not bearer_token and access_key_id and secret_access_key:
        credenciais = {
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
            "aws_session_token": session_token,
        }

    return ChatBedrockConverse(
        model_id=model_id,
        region_name=region_name,
        max_retries=2,
        **credenciais,
    )
