# Publicação de versão

1. Execute `make check` em Python 3.12.
2. Confirme que `git status` está limpo.
3. Atualize `__version__`, `pyproject.toml` e `CHANGELOG.md`.
4. Gere novamente `SHA256SUMS`.
5. Crie uma tag assinada quando possível.
6. Anexe o ZIP e seu SHA-256 à release do GitHub.
7. Nunca anexe credenciais, banco SQLite ou arquivos de `/etc/telegram-assistant`.
