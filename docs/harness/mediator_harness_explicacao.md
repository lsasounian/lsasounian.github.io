# Agente Mediador com Harness — LangChain + LangGraph + MCP

## Arquivos

| Arquivo | Descrição |
|---|---|
| `mediator_harness.py` | Versão standalone (roda em qualquer runtime: local, ECS, FastAPI) |
| `mediator_harness_agentcore.py` | Versão adaptada para **AWS Bedrock AgentCore Runtime** |

## Visão geral

O código implementa um **agente mediador (supervisor)** que:

1. Captura a intenção do usuário via LLM com saída estruturada.
2. Roteia a requisição para um **agente especialista** exposto via **MCP** (Streamable HTTP).
3. Executa tudo isso dentro de um **harness** — uma camada de infraestrutura que controla ciclo de vida, resiliência, limites e observabilidade da execução.

O grafo (LangGraph) resolve *o que fazer*. O harness resolve *como, quando e sob quais limites* isso é executado. Essa separação é o ponto central do design.

---

## O que é "harness" (harnessing)

Em sistemas de agentes, **harness** é o termo usado para a camada que "arreia" (do inglês *to harness* = arrear, controlar) a execução de um modelo ou grafo de agentes. É a diferença entre:

- **O agente** — o LLM/grafo que decide *o que fazer* (classificar intenção, chamar uma tool, gerar uma resposta).
- **O harness** — o código determinístico ao redor que decide *se, quando e como* essa decisão é executada.

Isso existe porque um LLM, por mais bem promptado que esteja, é um componente **não determinístico e falível**: pode alucinar uma rota inexistente, pode demorar mais que o esperado, pode chamar uma tool que está fora do ar. O harness é a fronteira de confiança entre o "raciocínio" do agente e o resto do sistema de produção.

Na prática, um harness normalmente cobre cinco frentes:

### 1. Ciclo de vida (lifecycle)
Controla o que acontece **antes** da primeira requisição e **depois** da última — não é o request/response em si, é a borda do processo.
- `startup()`: conecta ao MCP, faz *warmup* de tools, aquece conexões (o mesmo racional do seu `__warmup__` sentinel no AgentCore).
- `shutdown()`: fecha conexões de forma limpa, loga métricas finais.
- No código, isso é exposto como um `@asynccontextmanager` (`lifespan`) justamente para plugar direto no lifespan do FastAPI/AgentCore — o harness não deveria ser algo que o desenvolvedor precisa lembrar de chamar manualmente em cada rota.

### 2. Controle de execução (execution control)
O LLM não tem noção de "budget". O harness impõe isso de fora:
- **Timeout em cascata**: `total_timeout_s` (budget da requisição inteira) sempre maior que `specialist_timeout_s` (budget por chamada individual) — se cada especialista pudesse consumir o timeout total, uma cadeia de 2-3 chamadas estouraria o SLA.
- **Limite de iterações** (`max_iterations`): protege contra loops no grafo (ex.: classify → specialist → classify de novo por decisão errada do LLM). Esse limite é verificado no *edge condicional* (`_route_after_classify`), não dentro do nó — ou seja, é o harness controlando o grafo de fora, não o grafo se autolimitando.

### 3. Resiliência
O harness assume que **toda chamada externa vai falhar em algum momento** e projeta para isso:
- **Retry com backoff exponencial + jitter**: `delay = base * 2^attempt + jitter`. O jitter evita *thundering herd* quando múltiplas instâncias do mediador retentam ao mesmo tempo contra um especialista que acabou de cair.
- **Circuit breaker por rota**: cada especialista tem seu próprio breaker (`CLOSED` → `OPEN` → `HALF_OPEN`). Isso isola falhas — um especialista de billing degradado não deve consumir retries indefinidamente nem afetar a rota de RAG. É o mesmo princípio de *bulkhead isolation* que você já aplica no seu stack ECS.
- **Fallback gracioso**: quando o breaker está `OPEN` ou os retries se esgotam, a requisição não quebra — ela degrada para uma resposta genérica do LLM roteador, sem tools.

### 4. Guardrails
O harness não confia cegamente na saída do LLM:
- **Allowlist de rotas**: mesmo que o classificador de intenção "alucine" uma rota que não existe (`route: "cancelamento_de_universo"`), o harness reescreve para `fallback` antes de qualquer chamada real acontecer.
- **Threshold de confiança**: `confidence < 0.5` força fallback, independente da rota escolhida — isso captura casos em que o modelo está "chutando".
- **Validação de entrada**: tamanho máximo de payload, string vazia — barato de fazer, evita chamadas desperdiçadas ao LLM.

Essa camada é o que normalmente se chama de *LLM firewall*: valida entrada e saída do modelo como se ele fosse um componente não confiável — porque é.

### 5. Observabilidade
Tudo que passa pelo harness é medível de forma centralizada, em vez de espalhado pelos nós do grafo:
- `_metrics` agrega contadores (`requests`, `retries`, `fallbacks`) num único lugar.
- `thread_id` é propagado desde a entrada (`run()`) até o checkpointer do LangGraph — o mesmo padrão de correlação que você usa com OpenTelemetry (`thread_id` como correlation ID entre spans).
- `latency_ms` é medido no envelope externo (`run()`), não dentro dos nós — isso garante que o número reportado reflita o tempo real percebido pelo chamador, incluindo overhead do harness, não só o tempo de LLM.

---

## Por que separar harness do grafo (e não colocar tudo em nós)

Uma alternativa seria colocar retry/timeout/circuit breaker *dentro* dos próprios nós do LangGraph. O motivo para não fazer isso:

- **Testabilidade**: o grafo puro pode ser testado com mocks simples, sem precisar simular falhas de rede, timeouts ou circuit breakers abertos.
- **Reuso**: o mesmo harness (`AgentHarness`) pode envolver grafos diferentes sem duplicar lógica de resiliência em cada um.
- **Camada de confiança única**: toda decisão de "isso é seguro executar?" fica em um lugar, auditável, em vez de espalhada entre nós do grafo escritos por pessoas diferentes.
- **O grafo representa fluxo de raciocínio; o harness representa política operacional.** Misturar os dois torna o grafo difícil de ler (é raciocínio de negócio ou é engenharia de confiabilidade?) e dificulta ajustar SLAs sem tocar na lógica de roteamento.

---

## Componentes do código

| Componente | Papel |
|---|---|
| `HarnessConfig` | Contrato imutável de limites operacionais (timeouts, retries, allowlist) |
| `CircuitBreaker` | Isola falhas por especialista (estados CLOSED/OPEN/HALF_OPEN) |
| `IntentClassification` | Saída estruturada e validável do roteador (Pydantic) |
| `MediatorState` | Estado do grafo LangGraph (mensagens, intenção, resultado, erros) |
| `AgentHarness.startup/shutdown` | Ciclo de vida: warmup de MCP, pré-carregamento de tools por rota |
| `AgentHarness._validate_input/_validate_route` | Guardrails de entrada e saída |
| `AgentHarness._call_with_resilience` | Retry + backoff + jitter + circuit breaker, usado em toda chamada MCP |
| `_classify_intent` (nó) | Roteador: LLM com `with_structured_output` |
| `_invoke_specialist` (nó) | Chama a tool MCP da rota escolhida, sob o harness |
| `_fallback` (nó) | Degradação graciosa sem tools |
| `_route_after_classify` (edge condicional) | Aplica `max_iterations` e decide specialist vs. fallback |
| `AgentHarness.run()` | Único ponto de entrada público — todo o resto passa por aqui |

---

## Harness × AWS Bedrock AgentCore

O AgentCore Runtime já entrega **parte** do harness como serviço gerenciado. O mapeamento por camada define o que delegar à plataforma e o que continua no seu código:

| Camada do harness | AgentCore cobre? | Detalhe |
|---|---|---|
| Ciclo de vida | ✅ Parcial | Idle timeout (default 15 min) e max lifetime (default 8h) por sessão, configuráveis via `LifecycleConfiguration` (CDK). O contrato de `/ping` é seu. |
| Isolamento | ✅ | MicroVM por sessão: CPU, memória e filesystem isolados. Blast radius entre **usuários** — não substitui o breaker entre **especialistas**. |
| Controle de execução | ❌ | Timeout budget em cascata, `max_iterations` — lógica de aplicação, roda no seu container. |
| Resiliência | ❌ | Circuit breaker, retry + backoff + jitter — por sua conta. |
| Guardrails | ✅ Parcial | AgentCore Identity (M2M OAuth2/JWT) e Gateway (auth por target) reduzem a superfície. Allowlist de rotas continua como defesa em profundidade. |
| Observabilidade | ✅ Parcial | `ActiveSessionCount` no namespace `AWS/Bedrock-AgentCore`; logs em `/aws/bedrock-agentcore/runtimes/...`. Correlação por `session_id` até o grafo é sua. |

### O contrato de `/ping` (armadilha crítica)

O Runtime decide o idle timeout com base no status reportado pelo seu handler de `/ping` (`Healthy` / `HealthyBusy`) e no campo `time_of_last_update`, que deve refletir **quando o status mudou pela última vez** — não o instante do ping.

**A armadilha**: se o handler atualizar `time_of_last_update` a cada ping, o tempo ocioso reportado nunca avança, o idle timeout nunca dispara, e as sessões vivem até o `MaxLifetime` (8h) — esgotando a cota de sessões da conta silenciosamente sob carga.

**A solução no código**: `ping_status()` deriva o status de um contador de requisições em andamento (`_busy_sessions`), incrementado/decrementado dentro de `run()` com `finally`. As transições de status acontecem apenas quando o trabalho real começa e termina.

### O `__warmup__` sentinel

Padrão para eliminar cold start: um valor sentinela (`"__warmup__"`) que nunca aparece em input real é enviado como invocação fake logo após o container subir. O entrypoint checa esse valor como **primeira instrução** (fast-path, custo de uma comparação de string) e dispara todo o custo de inicialização — conexão MCP, carregamento de tools, obtenção de token — antes da primeira requisição de usuário. O `warmup()` é idempotente, e há um aquecimento defensivo inline caso o tráfego real chegue antes do sentinel.

### Diferenças da versão AgentCore

1. **`BedrockAgentCoreApp`** substitui o servidor genérico — porta 8080, decorators `@app.entrypoint` e `@app.ping`, contrato do Runtime.
2. **`session_id` do Runtime == `thread_id` do LangGraph** — o valor do header `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` é propagado direto para o checkpointer, dando correlação 1:1 entre logs do CloudWatch, estado do grafo e spans OTel.
3. **Timeout budget alinhado à plataforma** — `total_timeout_s` (120s) muito abaixo do idle timeout do Runtime (900s). Se o harness estourar o limite da plataforma, quem mata a sessão é o Runtime, e você perde o controle sobre o erro retornado ao chamador.
4. **AgentCore Gateway como único endpoint MCP** — em vez de N servers no `MultiServerMCPClient`, um único URL do Gateway com bearer token (`GatewayTokenProvider` com cache e lock). O Gateway mantém sessões MCP com estado (`Mcp-Session-Id`) e SSE, eliminando reconexão manual por especialista. O agrupamento de tools por prefixo de rota (`billing__get_invoice`) permanece.
5. **`StopRuntimeSession` como cleanup explícito** — payload com `end_session: true` termina a sessão imediatamente via boto3 em vez de esperar o idle timeout, liberando cota. Requer IAM `bedrock-agentcore:StopRuntimeSession`; a chamada é idempotente.

### Limitação conhecida: estado do breaker por microVM

Como cada sessão roda em sua própria microVM, o estado do `CircuitBreaker` é **por instância**: uma falha detectada numa sessão não protege as demais. Em volume alto, externalize o estado do breaker para Redis ou DynamoDB (mesmo padrão de distributed locks com `SET NX`), transformando-o num breaker distribuído por rota.

---

## Pontos de extensão para produção

- Trocar `MemorySaver` por `RedisSaver` (persistência entre processos, mesmo padrão dos 3 clusters Redis do seu stack). Na versão AgentCore, combinar com AgentCore Memory para memória de longo prazo.
- Instrumentar `run()` e `_call_with_resilience` com spans OpenTelemetry + X-Ray, propagando `thread_id`/`session_id` como correlation ID.
- Substituir o stub do `GatewayTokenProvider` por AgentCore Identity ou pelo `TokenManager` (RS256) existente — client credentials para M2M.
- Externalizar o estado do `CircuitBreaker` para Redis/DynamoDB (breaker distribuído por rota — ver limitação da microVM acima).
- Mover `_tools_by_route` para um cache com TTL, permitindo reconexão a servidores MCP que ficaram fora do ar sem precisar reiniciar o processo.
- Expor `_metrics` como métricas EMF/CloudWatch em vez de contadores em memória, complementando o `ActiveSessionCount` nativo do Runtime.
