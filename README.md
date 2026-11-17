# AIMO — investigação de GMUD em AWS Lambda

Esta Lambda recebe uma GMUD implementada, consulta o Datadog para todas as
entidades extraídas, compara a saúde com a baseline anterior à mudança e
retorna a análise na própria resposta. O agendamento fica a cargo do
orquestrador e o state do grafo é preservado pelo checkpointer do LangGraph.

## Fluxo

1. recebe a GMUD em `body.message` ou `gmud_payload`;
2. desserializa `txt_detl_soli_autm` e extrai as entidades com Bedrock;
3. consulta as dependências dos sistemas extraídos pela API oficial do Datadog,
   usando o nome original e as variantes lowercase com hífen e sem espaços;
4. acrescenta os serviços que chamam os sistemas consultados e transforma todos
   os alvos em filtros específicos do Datadog;
5. coleta uma baseline por alvo antes da implementação;
6. coleta logs, spans e métricas da janela atual para cada alvo;
7. compara as janelas e mantém o histórico no state do LangGraph;
8. avalia com GenAI a relação entre as degradações e a GMUD;
9. retorna extração, snapshots, comparações e correlação pelo handler.

Somente os checkpoints técnicos do LangGraph são persistidos no DynamoDB. Não
há tabelas de resultado da análise e a função não cria agendamentos no
EventBridge.

## Evento

`body`, `message` e `txt_detl_soli_autm` também podem ser strings JSON.

```json
{
  "body": {
    "window_minutes": 60,
    "message": {
      "cod_soli_autm": "CHG3094365",
      "txt_dado_idt_memo": "changeImplement",
      "txt_detl_soli_autm": "{\"number\":\"CHG3094365\",\"sys_id\":\"...\",\"work_start\":\"2026-08-10 19:50:02\",\"service\":\"payment-api\",\"environment\":\"production\",\"description\":\"...\"}"
    }
  }
}
```

Por padrão, a janela analisada corresponde aos últimos 60 minutos, limitada ao
período posterior à implementação. Nas chamadas seguintes da mesma GMUD, o
grafo retoma do último `last_window_end` salvo no checkpoint. O orquestrador
também pode informar uma janela exata:

```json
{
  "gmud_payload": {
    "number": "CHG3094365",
    "work_start": "2026-08-10T19:50:02Z",
    "service": "payment-api"
  },
  "window_from": "2026-08-10T20:00:00Z",
  "window_to": "2026-08-10T21:00:00Z"
}
```

## Resposta

O `body` da resposta contém:

- identificação da mudança e ambiente;
- entidades extraídas;
- `entity_results`, com baseline, snapshot, comparação e trajetória por entidade;
- `degradation`;
- `gmud_correlation` produzida pelo agente.

## Variáveis de ambiente

```dotenv
DD_API_KEY=...
DD_APP_KEY=...
DD_SITE=datadoghq.com
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=...
LANGGRAPH_CHECKPOINT_TABLE=...
LANGGRAPH_CHECKPOINT_TTL_SECONDS=2592000

INVESTIGATION_BASELINE_MINUTES=60
INVESTIGATION_WINDOW_MINUTES=60
INVESTIGATION_MAX_ENTITIES=0
DEGRADATION_LATENCY_PERCENT=50
DEGRADATION_ERROR_RATE_DELTA=0.03
```

`INVESTIGATION_MAX_ENTITIES=0` significa sem limite. Um valor positivo pode ser
usado como proteção operacional contra payloads excepcionalmente grandes.

O cliente oficial `datadog-api-client` lê `DD_API_KEY` e `DD_APP_KEY`
diretamente do ambiente. `DD_SITE` seleciona o site da organização, por exemplo
`datadoghq.com`, `datadoghq.eu` ou `us3.datadoghq.com`.

O módulo `collector/datadog_collector.py` usa o SDK oficial
`datadog-api-client` para consultar logs, spans, métricas e dependências de
serviços. A consulta de dependências exige um ambiente APM disponível no
Datadog; os serviços que chamam cada sistema consultado são incluídos nos alvos
monitorados.

## Imagem Lambda

```bash
docker build --platform linux/amd64 -t aimo-investigation-lambda .
```

O handler configurado na imagem é `src.codes.lambda_handler.lambda_handler`.
O template SAM provisiona a função e uma tabela DynamoDB exclusiva para os
checkpoints do LangGraph. A tabela usa `PK`/`SK`, criptografia, recuperação
point-in-time, retenção ao excluir a stack e TTL padrão de 30 dias:

```bash
sam build
sam deploy --guided
```
