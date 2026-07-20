# Modelo de ameaça

## Ativos protegidos

- token do bot Telegram;
- chave da Groq;
- agenda persistida;
- integridade dos compromissos;
- demais serviços da VPS;
- Docker socket, Traefik, n8n e arquivos de configuração do host.

## Fronteiras de confiança

```text
Conta Telegram → Telegram Bot API → processo Python → SQLite
                                      └──────────────→ Groq API
```

Telegram, conteúdo digitado pelo usuário, títulos persistidos e respostas da LLM
são entradas não confiáveis. SQLite só recebe dados depois de validação de domínio.

## Dados enviados à Groq

Em cada interação de linguagem natural, o processo envia um snapshot limitado da
agenda. Por padrão: próximos 30 dias e no máximo 50 eventos.

Campos enviados:

- ID interno do evento;
- título;
- início;
- término;
- janela consultada;
- indicador de truncamento.

Telegram IDs, chat ID, tokens, credenciais, caminhos da VPS e metadados internos
não são enviados.

## Invariantes

1. Uma atualização não autorizada é descartada antes da LLM e do banco.
2. A LLM nunca executa uma operação nem recebe ferramentas.
3. O snapshot enviado à LLM é limitado e contém somente campos permitidos.
4. Se a agenda não puder ser consultada, a mensagem não é enviada à LLM.
5. Toda alteração suportada exige confirmação humana.
6. Tokens de confirmação são de uso único, expiram e ficam vinculados ao usuário.
7. O processo não pode ler Docker socket, Traefik, SSH ou diretórios Docker.
8. Segredos não aparecem em argumentos, Git ou logs.
9. Toda interação autorizada recebe telemetria compacta da Groq, quando o banco está disponível.
10. Cada compromisso ativo possui lembretes persistentes de 30 e 15 minutos, reivindicados atomicamente.

## Ameaças e controles

### Descoberta do bot por terceiro

Controle: `user.id`, `chat.id` e tipo privado em allowlist. O bot não responde a
usuários desconhecidos e grupos são desabilitados no BotFather.

### Prompt injection no texto ou nos títulos

Controle: texto e snapshot serializados como dados JSON, system prompt restritivo,
schema estrito, allowlist de ações e validação local. Títulos podem ser usados como
referência factual, mas nunca como instrução. Nenhuma ferramenta é enviada à API.

### Exfiltração excessiva da agenda

Controle: janela temporal e quantidade de eventos configuráveis, máximo rígido de
90 dias e 100 eventos, campos permitidos por DTO e ausência de IDs do Telegram ou
metadados internos.

Risco residual: títulos e horários enviados no snapshot são processados pela Groq.
Não armazene segredos ou conteúdo altamente confidencial na agenda.

### Criação, edição ou exclusão indevida

Controle: confirmação com token aleatório, hash no banco, vínculo ao usuário,
expiração e consumo atômico na mesma transação da alteração. Edição e exclusão também recriam ou removem lembretes de forma transacional.

### SQL injection

Controle: todas as entradas variáveis usam parâmetros SQLite. Nenhum SQL é criado
pela LLM ou pelo usuário.

### Comprometimento do processo

Controle: usuário sem privilégios, capabilities vazias, `NoNewPrivileges`,
`ProtectSystem=strict`, `ProtectHome`, namespaces restritos, limites de CPU/RAM e
caminhos sensíveis inacessíveis.

### Movimento lateral para containers

Controle: usuário fora do grupo Docker, Docker socket inacessível e `/var/lib/docker`
bloqueado no unit.

### Roubo de segredos

Controle: `LoadCredential=`, arquivos `0600` de root, recusa de symlinks e logs
sanitizados. Em suspeita de vazamento, rotacionar imediatamente no BotFather e
Groq Console.

### Supply chain

Controle: dependências mínimas, versões e hashes fixos, somente wheels binários e
sem atualização automática de pip durante deploy.

### Abuso de quota

Controle: rate limit local do bot, tamanho máximo de mensagem, saída máxima da LLM,
telemetria de uso e exibição de limites após cada interação autorizada.

RPD e TPM vêm do último header válido da Groq. RPM e TPD são estimativas locais e
podem divergir se a mesma organização ou projeto for usado por outra aplicação.

## Riscos residuais

- root comprometido pode ler banco e credenciais;
- conta Telegram comprometida possui a identidade autorizada;
- Telegram e Groq processam conteúdo enviado;
- títulos e horários do snapshot deixam a VPS;
- outra aplicação usando o mesmo projeto Groq altera os saldos reais;
- o host possui outros serviços públicos que precisam de auditoria independente;
- regras de egress ainda não restringem o processo exclusivamente por domínio.

## Resposta a incidente

1. Pare o serviço.
2. Revogue o token no BotFather.
3. Revogue a chave no Groq Console.
4. Preserve logs e snapshot do banco.
5. Verifique login SSH, usuários, units, cron, containers e Docker socket.
6. Reinstale credenciais novas somente após determinar o vetor de entrada.
7. Restaure o serviço com banco validado e execute `security-check.sh`.
