# Contribuindo

## Fluxo recomendado

1. Crie um fork e uma branch curta.
2. Não inclua tokens, chaves, bancos SQLite ou logs privados.
3. Mantenha decisões de segurança e negócio no código local, não no prompt da LLM.
4. Adicione testes para mudanças em banco, domínio, confirmações ou lembretes.
5. Execute `make check` antes do pull request.

## Regras de implementação

- Toda escrita deve exigir confirmação humana.
- A LLM não recebe ferramentas, shell, SQL ou acesso a arquivos.
- Entradas devem ser validadas localmente.
- Queries SQLite devem ser parametrizadas.
- Migrações devem preservar bancos existentes e ser testadas.
- Dependências novas precisam de justificativa e versão fixada.
- Logs não devem conter texto integral do usuário, tokens ou respostas brutas sensíveis.

## Pull requests

Descreva:

- problema resolvido;
- comportamento anterior e novo;
- riscos e compatibilidade;
- testes executados;
- impacto em migração, credenciais e serviços.
