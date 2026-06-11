# lsasounian.github.io

Site estático publicado via GitHub Pages que funciona como uma base de conhecimento técnica (**DevDocs**), reunindo documentação de referência sobre arquiteturas de sistemas multi-agentes com **LangChain**, **LangGraph**, **MCP (Model Context Protocol)** e **AWS**.

🔗 Publicado em: https://lsasounian.github.io

## Estrutura

```
.
├── index.html                  # Hub principal (DevDocs) — lista e busca todos os documentos
├── assets/
│   └── css/theme.css           # Design system compartilhado (paleta, tipografia, componentes)
└── docs/
    ├── mcp-fastmcp-guide.html  # Guia completo de MCP & FastMCP
    ├── a2a-protocol.html       # Guia do protocolo Agent-to-Agent (A2A)
    ├── a2a-aws-docs.html       # Padrões A2A na AWS via API Gateway (SigV4 / Bearer JWT)
    └── langchain/              # Série "Multi-Agent AI Systems"
        ├── index.html          # Hub da série
        ├── 01-langchain-langgraph.html
        ├── 02-memoria-distribuida.html
        ├── 03-arquitetura-tres-contas.html
        └── 04-memoria-redis-dynamo.html
```

## Documentos

**Frameworks & Bibliotecas**
- *MCP & FastMCP* — os 3 primitivos do MCP, transportes, tools/resources/prompts, autenticação, deploy e limites.

**A2A — Agent-to-Agent Protocol**
- *A2A Protocol* — funcionamento interno do protocolo, integração com LangGraph e API Gateway.
- *A2A na AWS* — padrões cross-account via API Gateway com SigV4 e Bearer JWT, com Terraform.

**Multi-Agent AI Systems (LangChain/LangGraph)**
1. *LangChain vs LangGraph* — diferença de responsabilidades, StateGraph, padrão Supervisor/Especialista e Agent-as-a-Tool via MCP.
2. *Camadas de Memória* — memória distribuída, Redis como checkpointer, RAG com OpenSearch e memória semântica de longo prazo.
3. *Arquitetura 3 Contas* — separação AWS em Conta Supervisora, Conta de Agentes (ECS + MCP) e Conta de BFFs.
4. *Memória Redis & DynamoDB* — clusters Redis com TTLs isolados, tabelas DynamoDB, checkpointer LangGraph e arquivamento de threads.

## Identidade visual

Todas as páginas compartilham o mesmo design system definido em `assets/css/theme.css`: tema escuro (navy), fontes Inter + JetBrains Mono, layout com sidebar de navegação fixa e componentes reutilizáveis (callouts, diagramas, blocos de código, tabelas, cards). Cada documento usa uma cor de destaque (`--accent`) própria para diferenciação visual.

## Rodando localmente

Por ser um site 100% estático, basta servir a pasta raiz com qualquer servidor HTTP:

```bash
python3 -m http.server 8000
```

E acessar `http://localhost:8000`.
