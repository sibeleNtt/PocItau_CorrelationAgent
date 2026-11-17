# Consultas Datadog na investigação

Para cada janela (baseline e atual), `DatadogQueryBuilder` recebe os alvos da
investigação e constrói uma consulta OR combinada para logs e outra para APM.
Os resultados dessas consultas são separados por sistema antes de calcular os
resumos.

As métricas usam o endpoint v2 `POST /api/v2/query/timeseries`. Todas as queries
dos alvos de uma janela são enviadas juntas; cada query recebe um nome
identificador, e a resposta é associada ao alvo/métrica pela posição da query e
`query_index`. A baseline e a janela atual são consultadas separadamente,
resultando normalmente em duas requisições HTTP de métricas por execução.

A classe `DatadogQueryBuilder` centraliza a construção das queries sem mudar os
cinco nomes de métricas nem os filtros por alvo.

Logs e spans são agrupados numa página por janela. Se a resposta indicar
paginação, `collection_error` informa que há resultados adicionais não
coletados; o fluxo não busca páginas extras automaticamente.

As consultas de dependências feitas durante a preparação da investigação são
separadas dessas requisições de telemetria.
