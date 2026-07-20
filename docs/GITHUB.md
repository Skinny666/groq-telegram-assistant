# Publicar no GitHub

Crie um repositório vazio no GitHub, sem README ou `.gitignore` automáticos. Depois, dentro desta pasta:

```bash
git init
git add .
git commit -m "Initial release: v0.4.0"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/telegram-assistant-secure.git
git push -u origin main
```

Com GitHub CLI:

```bash
gh repo create telegram-assistant-secure \
  --private \
  --source=. \
  --remote=origin \
  --push
```

Recomendações iniciais:

1. mantenha o repositório privado até revisar o histórico;
2. habilite **Private vulnerability reporting**;
3. proteja a branch `main` exigindo o workflow `Tests`;
4. não use GitHub Secrets para o token de produção se o deploy não depende de Actions;
5. crie a tag `v0.4.0` somente depois do primeiro push validado.

Para criar a tag:

```bash
git tag -a v0.4.0 -m "Telegram Assistant Secure v0.4.0"
git push origin v0.4.0
```
