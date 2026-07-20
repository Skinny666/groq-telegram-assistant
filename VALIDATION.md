# Relatório de validação — 0.4.0

Data: 20 de julho de 2026

## Ambiente utilizado

- Python 3.13.5 para validação local.
- Alvo de produção: Python 3.12 no Ubuntu 24.04.
- python-telegram-bot 22.8.
- httpx 0.28.1.

## Resultados

- `compileall`: aprovado.
- `unittest`: 27 testes aprovados.
- Sintaxe dos scripts shell: aprovada com `bash -n`.
- Migração sintética de banco v2 para v3: aprovada.
- Criação de dois lembretes por evento: aprovada.
- Edição substitui evento e lembretes: aprovada.
- Exclusão remove lembretes pendentes: aprovada.
- Reivindicação atômica de lembretes: aprovada.
- Tokens de confirmação com hash, vínculo e uso único: aprovados.
- Snapshot limitado da agenda e ação forçada: aprovados.
- Formatação compacta de limites: aprovada.
- Instalação com dependências fixadas por versão e hash: definida em `requirements.lock`.

## Comandos executados

```bash
PYTHONPATH=src python3 -m compileall -q src tests tools
PYTHONPATH=src python3 -m unittest discover -s tests -v
bash -n deploy/install.sh deploy/security-check.sh
```

## Limitações

- A validação automatizada não usa credenciais reais de Telegram ou Groq.
- A integração real depende das quotas e disponibilidade do provedor.
- O alvo oficial permanece Python 3.12; valide novamente na VPS antes de atualizar produção.
- Ferramentas externas de auditoria de vulnerabilidades devem ser executadas em ambiente com acesso à internet.
