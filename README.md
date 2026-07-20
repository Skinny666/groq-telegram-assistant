# Telegram Assistant Secure

Assistente privado de agenda via Telegram, Groq e SQLite. Foi projetado para rodar em uma VPS com `systemd`, usando long polling: não publica webhook, não abre porta e não precisa de domínio ou rota no Traefik.

## Principais recursos

- Criação de compromissos em português natural.
- Conversas em várias etapas quando faltam data, horário, título ou alvo.
- Consulta da agenda e sugestão determinística de horários livres.
- Edição e exclusão por ID, título ou linguagem natural.
- Confirmação obrigatória antes de criar, editar ou excluir.
- Dois lembretes persistentes por compromisso: 30 e 15 minutos antes.
- Migrações automáticas do SQLite, preservando eventos existentes.
- Acesso restrito a um único Telegram user ID e somente em conversa privada.
- Limites da Groq exibidos após interações autorizadas.
- Serviço isolado por `systemd`, sem acesso ao Docker socket.

## Exemplo de uso

```text
Você: Quero agendar
Bot: O que você quer agendar?

Você: Falar com a Nadine antes de movimentar a apólice de julho
Bot: Qual dia e horário devo usar?

Você: Dia 27 às 10h
Bot: Confirmar compromisso?
```

Também funciona em uma mensagem:

```text
Agende falar com a Nadine dia 27 às 10h por 45 minutos.
Mude o compromisso com a Nadine para 11h.
Exclua o compromisso com a Nadine.
O que tenho na próxima semana?
Tenho horário livre dia 27 à tarde?
```

## Arquitetura

```text
Telegram Bot API
       │ long polling
       ▼
Processo Python ─────────────► Groq API
       │                       interpretação estruturada
       │
       ├── SQLite: eventos, confirmações, telemetria e lembretes
       └── Worker systemd: envia lembretes 30 e 15 minutos antes
```

A Groq interpreta a intenção e extrai campos em JSON Schema estrito. Ela não recebe shell, SQL, ferramentas, Docker ou acesso direto ao Telegram. Toda alteração é validada e executada pelo código local somente depois da confirmação do usuário.

## Comandos

```text
/start      ajuda
/agenda     próximos compromissos
/agendar    criar compromisso
/editar     editar compromisso
/excluir    excluir compromisso
/horarios   sugerir horários livres
/limites    consultar uso da Groq
/status     verificar o bot
```

Aliases mantidos por compatibilidade: `/listar`, `/livre`, `/cancelar` e `/ajuda`.

## Requisitos

- Ubuntu 24.04 ou distribuição com `systemd`.
- Python 3.12.
- Bot criado no BotFather.
- Chave de API da Groq.
- Conversa privada com o bot.

## Preparação no Telegram

1. Crie o bot com `/newbot` no BotFather.
2. Desative grupos com `/setjoingroups` → `Disable`.
3. Não habilite inline mode.
4. Nunca salve o token em Git, `.env` público ou histórico do shell.

Para descobrir seu Telegram user ID:

```bash
python3 tools/discover_user_id.py
```

O token é solicitado de forma oculta e não é salvo pelo script.

## Instalação na VPS

Clone o repositório e revise o instalador:

```bash
git clone https://github.com/SEU_USUARIO/telegram-assistant-secure.git
cd telegram-assistant-secure
less deploy/install.sh
less deploy/telegram-assistant-bot.service
```

Instale:

```bash
sudo bash deploy/install.sh
```

O instalador solicita de forma interativa:

- token do bot Telegram;
- chave da Groq;
- Telegram user ID autorizado.

Os segredos são gravados como arquivos `0600`, pertencentes ao root, e entregues aos serviços via `LoadCredential=`. O instalador não altera UFW, Traefik, Docker ou n8n.

## Operação

```bash
sudo systemctl status telegram-assistant-bot.service
sudo systemctl status telegram-assistant-reminder.timer
sudo journalctl -u telegram-assistant-bot.service -n 100 --no-pager
sudo journalctl -u telegram-assistant-reminder.service -n 100 --no-pager
```

Verificação de isolamento:

```bash
sudo bash deploy/security-check.sh
```

## Atualização

Antes de trocar de versão:

```bash
sudo systemctl stop telegram-assistant-bot.service
sudo systemctl stop telegram-assistant-reminder.timer
sudo cp -a /var/lib/telegram-assistant/events.db \
  /var/lib/telegram-assistant/events.db.backup-$(date +%Y%m%d-%H%M%S)
```

Depois de atualizar os arquivos, execute novamente o instalador ou copie o código validado para `/opt/telegram-assistant` e reinicie os serviços. As migrações do SQLite são executadas na inicialização.

## Desenvolvimento

Crie o ambiente:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install \
  --only-binary=:all: \
  --require-hashes \
  -r requirements.lock
```

Execute as verificações:

```bash
make check
```

Ou diretamente:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m compileall -q src tests tools
bash -n deploy/install.sh deploy/security-check.sh
```

## Privacidade

Mensagens em linguagem natural podem enviar à Groq um snapshot limitado da agenda. Por padrão, ele contém no máximo 50 eventos dos próximos 30 dias, com:

- ID interno;
- título;
- início;
- término;
- intervalo consultado;
- indicador de truncamento.

Não são enviados Telegram user ID, chat ID, token, chave Groq, caminhos da VPS, status dos lembretes ou metadados do banco.

Não use títulos de agenda para armazenar senhas, tokens, chaves privadas, dados bancários ou conteúdo altamente confidencial.

## Segurança

Controles principais:

- autorização antes de qualquer chamada à Groq ou ao banco;
- chat privado e usuário fixo;
- JSON Schema estrito e rejeição de campos extras;
- SQL parametrizado;
- confirmação de uso único, expirada e vinculada ao usuário;
- usuário Linux sem login e fora do grupo Docker;
- credenciais por `LoadCredential=`;
- dependências fixadas por versão e hash;
- serviços sem porta pública;
- limites de CPU, memória, processos e arquivos no `systemd`.

Leia [SECURITY.md](SECURITY.md) e [THREAT_MODEL.md](THREAT_MODEL.md) antes de expor o projeto a outros usuários.

## Limitações conhecidas

- O projeto foi desenhado para um único usuário autorizado.
- O contexto de conversa fica em memória e pode ser perdido quando o bot reinicia.
- Datas e horários dependem da interpretação da Groq; validações locais impedem operações inválidas, mas frases ambíguas podem exigir perguntas adicionais.
- A mesma chave Groq usada por outros sistemas altera os saldos reais de quota.
- Telegram e Groq processam o conteúdo enviado a eles.

## Licença

MIT. Consulte [LICENSE](LICENSE).
