# Harness mediador single-agent com skills — resumo e aprendizados

## Contexto

Sessão de design de um novo padrão de agente mediador: um **único agente** que ativa
skills especializadas por domínio (em vez de delegar pra agentes filhos via A2A, como
no padrão Supervisor/Specialist já existente). O ponto de partida foi reconhecer que
isso generaliza o `classify_intent` antes de bindar tools já implementado no child
agent template — só que de um gate binário (KB sim/não) pra N skills.

## 1. Multi-agente A2A vs single-agent skill harness

| Dimensão | Multi-agente A2A (existente) | Single-agent skill harness (novo) |
|---|---|---|
| Isolamento de falha | Alto — processo/timeout por agente filho | Baixo — falha de skill derruba o turno inteiro se não tratada |
| Latência | Round-trip HTTP/A2A + JWT M2M por chamada | Sem round-trip externo |
| Compartilhamento de contexto | Serializado via mensagem A2A | Nativo — mesmo `state` do LangGraph |
| Auth entre especialistas | JWT Client Credentials por chamada | Nenhuma — single trust boundary |
| Budget de timeout | Cascata (25s → 22s supervisor → 15s A2A) | Um único budget por turno |
| Quando faz mais sentido | Especialistas com contratos fortes, scaling independente | Tarefas que compartilham muito contexto e alternam foco rápido |

## 2. Arquitetura do grafo

```
Entrada → Router (embedding + fallback LLM) → Skill loader (injeta instruções + MCPs)
   → Agent node ⇄ Tool node (loop de tool calls via MCP)
   → Exit check → Fim  |  ↻ nova skill (volta pro Router)
```

Componentes:

- **Router**: decide qual skill ativar. Com catálogo pequeno (dezenas de skills),
  LLM direto já resolve — embeddings só compensam com catálogo grande.
- **Skill loader**: injeta as instruções completas da skill (efêmeras, não persistidas
  no `state["messages"]`) e resolve quais MCPs/tools bindar.
- **Agent node**: `bind_tools` dinâmico, só com as tools da skill ativa.
- **Tool node**: executa as chamadas MCP.
- **Exit check**: decide entre encerrar ou rotear pra outra skill no mesmo turno.

## 3. Formato de skill (paralelo ao SKILL.md)

```yaml
# skill.yaml
name: billing_specialist
description: >
  Resolve disputas de cobrança, reembolsos e parcelamento.
mcp_servers: [billing-mcp, payments-mcp]
allowed_tools: [get_invoice, issue_refund, get_payment_status]
```

O paralelo com progressive disclosure: `description` é sempre visível pro router
(entra no embedding); `instructions.md` só carrega quando a skill é selecionada.

## 4. State schema

```python
class MediatorState(TypedDict):
    messages: Annotated[list, add_messages]
    active_skill: Optional[str]
    skill_history: Annotated[list[str], operator.add]
    bound_tool_names: list[str]
    route_score: float
    skill_iterations: int
```

## 5. Bind dinâmico de tools

O núcleo técnico do padrão: `bind_tools` roda dentro do node, não na definição
estática do grafo — o conjunto de tools muda a cada seleção de skill.

```python
def agent_node(state, config):
    skill = skill_registry.get(state["active_skill"])
    tools = mcp_tool_cache.get_or_fetch(skill.mcp_servers, skill.allowed_tools)
    model_with_tools = base_model.bind_tools(tools)
    response = model_with_tools.invoke([SystemMessage(skill.instructions), *state["messages"]])
    return {"messages": [response], "bound_tool_names": [t.name for t in tools]}
```

## 6. Conexão MCP

- Lazy connect só aos servers que a skill ativa declara (não eager em todos no cold start).
- Pool de sessões MCP por nome de server, reusado entre skills que compartilham o mesmo MCP.
- MCP cross-account reusa o mesmo padrão da Redis facade (FastAPI + Lambda + mTLS).
- `traceparent` injetado manualmente antes do handshake mTLS (API Gateway não propaga sozinho).

## 7. Gestão de contexto

- Instruções da skill são efêmeras: entram como `SystemMessage` só nas invocações
  daquela skill, não persistem no `state["messages"]` — mesmo princípio do `idCliente`
  smuggled que é parseado e removido antes do LLM ver.
- Instruções repetidas entre chamadas dentro do turno são candidatas naturais a
  prompt caching da API.

## 8. Observabilidade

| Span attribute | Descrição |
|---|---|
| `skill.selected` | id da skill escolhida pelo router |
| `skill.route_score` | score de similaridade (quando roteamento é por embedding) |
| `skill.load_latency_ms` | tempo pra carregar instruções + resolver tools |
| `skill.iterations` | idas ao tool_node dentro da skill, detecta loop |
| `skill.reroute_count` | quantas vezes o exit_check voltou pro router no mesmo turno |

## 9. Skill registry — componentes gerais

Quatro peças: skill store (fonte da verdade), skill loader (cold start + hot reload),
skill index (busca), runtime lookup (o que o router chama).

**Regra chave**: o catálogo não pode estar preso na imagem do container, senão skill
nova exige redeploy do agente inteiro. Precisa ser dado, não código.

Duas formas concretas de implementar isso foram avaliadas (seções 10 e 11).

## 10. Opção A — AWS Agent Registry (serviço gerenciado real)

Confirmado por pesquisa: **AWS Agent Registry** é um serviço real do Bedrock
AgentCore, em preview desde abril/2026 — não é conceito hipotético. Mapeia quase
1:1 com o design de skill registry:

| Registry record | Onde entra no design |
|---|---|
| `name` + `recordVersion` | versionamento nativo por skill (ex: `cartoes` v2.1) |
| `recordType`: AGENT / MCP / SKILL / CUSTOM | skill é tipo de primeira classe |
| descriptor (nome, descrição) + Package/Repository + markdown opcional | metadado leve + artefato pesado fora do registro |
| busca híbrida nativa (semântica + keyword) | substitui o índice de embeddings Titan que desenharíamos manualmente |
| endpoint MCP próprio (`InvokeRegistryMcp`) | Main Agent consulta o registro pelo mesmo client MCP que usa pras tools |
| fluxo de aprovação (draft → submit → approve/deprecate) + EventBridge | governança opcional, útil em domínio regulado |

**Timing**: serviço em migração de namespace agora — `bedrock-agentcore` (preview)
descontinuado em 17/set/2026, usar `agent-registry` desde 6/ago/2026 se for
implementar do zero.

**Não confirmado**: se o campo Package/Repository aceita URI S3 diretamente ou é
mais orientado a git/gerenciador de pacote — verificar na referência da API antes
de comprometer o design de artefato.

**Consequência prática**: essa opção supera o mapeamento "AWS convencional"
(DynamoDB + S3 construído do zero) cogitado antes de confirmar que esse serviço
gerenciado existe — se disponível/aprovado internamente, é a escolha mais direta.

## 11. Opção B — self-hosted via DynamoDB + MCP

Caminho pra quando o AWS Agent Registry não está disponível. Replica o mesmo
modelo (name + version) com peças convencionais:

- **DynamoDB** single-table: PK `skill_id`, SK `v{n}` por versão + item `LATEST`
  como ponteiro, escritos atomicamente via `transact_write_items`.
- **MCP server próprio** (FastMCP) expondo `search_skills`, `get_skill`,
  `list_skills` como tools.
- **Busca sempre via API** — scan + filtro por palavra-chave, sem embedding, sem
  cache local. Só compensa trocar por GSI/vetor se o catálogo crescer muito além
  de dezenas/centenas de skills.
- **Hosting**: ALB + ECS Fargate recomendado em vez de Lambda + API Gateway, pelo
  mesmo motivo da migração WebSocket → SSE (transporte `streamable-http` do MCP
  tende a manter conexão mais longa que o timeout de 29s do API Gateway) —
  assumido, não confirmado com o padrão de uso real ainda.
- Se compartilhado entre contas, reusa o padrão da Redis facade (FastAPI + mTLS),
  só que o protocolo por cima é MCP em vez de REST.

Entregável gerado: `skill_registry_mcp_server.py`.

## 12. Agent registry — decisão

Quatro opções levantadas: (1) fallback local→A2A, (2) mediador exposto como agente
descobrível, (3) catálogo central entre múltiplos mediadores, (4) registry unificado
skill+agente remoto.

**Conclusão**: com um único agente centralizado (N=1), agent registry não se aplica —
o problema que esse padrão resolve ("qual desses N agentes atende essa requisição")
não existe com N=1. O skill registry já cobre a decisão equivalente dentro do agente.
Se um segundo mediador surgir no futuro, o mesmo formato `(id, description, ...)`
do skill registry generaliza sem precisar redesenhar nada agora — e, se a opção A
(AWS Agent Registry) for adotada, o `recordType: AGENT` já cobre esse caso nativamente
no mesmo registro, sem precisar de um serviço separado.

## 13. Tradução prática: Copilot Studio → LangGraph

| Copilot Studio (generative orchestration) | LangGraph |
|---|---|
| Topic / Skill | `Skill` dataclass no dict `SKILLS` |
| Frases-gatilho do topic | `intent_examples` |
| Knowledge source vinculada | tool de KB, bindada só na skill ativa |
| Orquestrador que decide o topic | `router_node` |
| Instruções do topic | `instructions`, concatenadas no prompt do `agent_node` |
| "Questione antes de seguir" | `interrupt()` + checkpointer |

Entregável gerado: `mediator_harness.py` — implementação completa com router, agent
node, tool node e 3 skills de exemplo (`cartoes`, `pf`, `seguros`), a partir do
system prompt real do harness em Copilot Studio.

## 14. Pendências / decisões em aberto

- Preencher `instructions` reais de `pf` e `seguros` (só `cartoes` veio do exemplo real).
- `clarify_node` está implementado mas **não conectado** ao grafo — falta decidir
  como o agente sinaliza "falta informação": via tool call (`ask_user`) que o LLM
  decide chamar, ou via checagem determinística em cima da resposta.
- Decidir entre Opção A (AWS Agent Registry gerenciado, se disponível/aprovado
  internamente) e Opção B (self-hosted DynamoDB + MCP) — muda quem mantém a infra
  de busca e governança.
- Se for Opção A: confirmar se Package/Repository aceita URI S3 direto.
- Se for Opção B: validar se `streamable-http` realmente precisa de conexão longa
  no padrão de uso real antes de comprometer com ALB + Fargate em vez de Lambda.
- Se o catálogo de skills crescer muito além de 3, revisitar roteamento/busca por
  embedding.

## Arquivos gerados nesta sessão

- `mediator_harness.py` — harness LangGraph com router, agent node, tool node e
  3 skills de exemplo.
- `skill_registry_mcp_server.py` — skill registry self-hospedado (Opção B),
  exposto via MCP, backed por DynamoDB.
