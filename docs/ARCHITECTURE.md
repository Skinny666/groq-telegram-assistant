# Arquitetura

## Componentes

- `bot.py`: autorização, comandos, fluxo conversacional e confirmações.
- `llm.py`: contrato com a Groq, prompt restrito e JSON Schema.
- `domain.py`: validações e modelos de negócio.
- `database.py`: persistência SQLite, migrações e transações.
- `availability.py`: cálculo determinístico de horários livres.
- `reminder_worker.py`: reivindicação e envio dos lembretes.
- `telegram_api.py`: cliente mínimo usado pelo worker.
- `config.py`: configuração e leitura segura de credenciais.
- `security.py`: rate limiter local.

## Fluxo de escrita

```text
Mensagem autorizada
  → snapshot limitado da agenda
  → classificação estruturada pela Groq
  → validação local
  → proposta com token de confirmação
  → clique do usuário
  → transação SQLite
```

A resposta da Groq nunca executa diretamente uma operação.

## Lembretes

Cada evento possui dois registros em `event_reminders`, com offsets de 30 e 15 minutos. O worker:

1. reivindica lembretes vencidos em transação;
2. envia pelo Telegram;
3. marca como enviado;
4. libera para nova tentativa quando ocorre falha transitória.

A reivindicação impede que duas execuções simultâneas enviem o mesmo lembrete.

## Banco

O SQLite usa `PRAGMA user_version` para migrações. A versão 3 adiciona lembretes múltiplos e ações pendentes de edição/exclusão. Migrações devem ser idempotentes e validadas com bancos sintéticos de versões anteriores.
