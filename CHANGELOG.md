# Changelog

Todas as mudanças relevantes deste projeto serão registradas neste arquivo.

## [0.4.0] - 2026-07-20

### Adicionado

- Edição de compromissos com confirmação.
- Exclusão por ID, título ou linguagem natural com confirmação.
- Dois lembretes persistentes: 30 e 15 minutos antes.
- Fluxo conversacional com contexto temporário para dados incompletos.
- Busca de alvo por título e resolução de resultados ambíguos.
- Migração automática do banco para `user_version=3`.

### Alterado

- `/agendar`, `/editar` e `/excluir` usam interpretação em linguagem natural.
- Mensagens e comandos foram simplificados para uso cotidiano.
- Formatação dos limites da Groq ficou mais legível.
- Instalador ajusta corretamente as permissões do ambiente virtual para o usuário do serviço.

### Segurança

- Criar, editar e excluir continuam exigindo confirmação de uso único.
- Lembretes são persistidos e reivindicados atomicamente para evitar duplicação.
- Credenciais permanecem fora do ambiente e são entregues por `LoadCredential=`.
