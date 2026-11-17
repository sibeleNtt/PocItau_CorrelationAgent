# Persistência do state

A Lambda não persiste documentos de resultado da investigação. O DynamoDB é
usado exclusivamente pelo `DynamoDBSaver` como backend de checkpoints do
LangGraph.

Cada GMUD recebe um `thread_id` determinístico derivado de `sys_id` ou `number`.
O LangGraph grava um checkpoint após cada etapa do grafo e restaura esse thread
na próxima invocação. O state mantém:

- GMUD e entidades extraídas;
- alvos e filtros de telemetria por entidade;
- resumo da baseline usado na comparação corrente;
- histórico de comparações por entidade;
- final da última janela analisada;
- trajetórias de degradação e última correlação.

O snapshot bruto da baseline não é armazenado no state. A cada execução, a
baseline é coletada novamente do Datadog para a mesma faixa horária da data do
dia anterior à implantação da GMUD. O checkpoint mantém apenas seu resumo e os
resultados das comparações. O cache de baseline por entidade foi desativado.

A tabela usa chave composta `PK`/`SK`, TTL, criptografia e recuperação
point-in-time. Os checkpoints são comprimidos e expiram após 30 dias por
padrão. A autenticação usa automaticamente a cadeia padrão do boto3.

O resultado funcional não é consultado diretamente da tabela. Cada invocação
devolve no `body` a análise corrente, incluindo `entity_results`, `degradation`
e `gmud_correlation`.

Não há agendamento interno no EventBridge. O orquestrador chama a Lambda na
frequência desejada; o grafo usa o checkpoint para começar a nova janela no
final da janela anterior.
