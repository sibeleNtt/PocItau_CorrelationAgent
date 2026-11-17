from pathlib import Path

def carregar_system_prompt(prompt_file: Path) -> str:
    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Arquivo de prompt não encontrado: {prompt_file}"
        )

    prompt = prompt_file.read_text(encoding="utf-8").strip()

    if not prompt:
        raise ValueError(
            f"O arquivo de prompt está vazio: {prompt_file}"
        )

    return prompt
