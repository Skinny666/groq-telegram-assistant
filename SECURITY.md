# Política de segurança

## Versões suportadas

Somente a versão mais recente da branch principal recebe correções de segurança.

## Como reportar uma vulnerabilidade

Não abra uma issue pública contendo tokens, chaves, conteúdo da agenda, caminhos privados ou detalhes exploráveis.

Use o recurso **Report a vulnerability** na aba **Security** do repositório. Inclua:

- versão ou commit afetado;
- cenário de ameaça;
- passos mínimos para reproduzir;
- impacto esperado;
- correção sugerida, quando houver.

Não inclua credenciais reais. Revogue imediatamente qualquer token ou chave que tenha sido exposta.

## Escopo prioritário

- bypass da autorização por Telegram user ID ou chat;
- execução de criação, edição ou exclusão sem confirmação;
- reutilização de token de confirmação;
- exposição de credenciais nos logs ou no repositório;
- acesso ao Docker socket ou a arquivos do host;
- SQL injection;
- exfiltração de dados além do snapshot documentado;
- repetição indevida de lembretes.

## Resposta a incidente

1. Pare os serviços.
2. Revogue o token no BotFather.
3. Revogue a chave no console da Groq.
4. Preserve os logs e uma cópia do banco.
5. Verifique acessos SSH, usuários, units, cron, containers e Docker socket.
6. Restaure somente depois de determinar o vetor e rotacionar os segredos.
