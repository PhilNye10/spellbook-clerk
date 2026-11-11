# GitHub setup and one-command updates

This guide moves the bot source into a **private** GitHub repository. Keep `.env`, `config.yaml`, and `data/` on the Ubuntu server; they are intentionally excluded from Git.

## 1. Create a private GitHub repository

On GitHub, create a new repository named `spellbook-clerk`. Set visibility to **Private**. Do not initialize it with a README, license, or `.gitignore` because this project already contains files.

## 2. Put the source on the Ubuntu server

Copy/extract this project into `/opt/mtg-table-bot` as you have done with previous ZIP updates. Do not overwrite your existing `.env`, `config.yaml`, or `data/` directory. Ensure the new `.gitignore`, `update.sh`, and `GITHUB_SETUP.md` are in `/opt/mtg-table-bot`.

From the server:

```bash
cd /opt/mtg-table-bot
```

If your current installation already has `.env`, `config.yaml`, and `data/`, leave them in place. If any are missing, stop and restore them before proceeding.

## 3. Initialize Git and push the initial version

Replace `YOUR_GITHUB_USERNAME` with your GitHub username and run:

```bash
cd /opt/mtg-table-bot

git init
git branch -M main
git add .
git status --short
```

**Before committing**, confirm `.env`, `config.yaml`, and `data/` are NOT listed as files to be committed. If any appear, stop; do not push secrets or database files.

Then:

```bash
git commit -m "Initial Spellbook Clerk source"
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/spellbook-clerk.git
git push -u origin main
```

GitHub may prompt you to authenticate. Use GitHub's browser/device sign-in or a Personal Access Token when prompted; do not put a token in a command, script, or this repository.

## 4. Update the bot in the future

Whenever a new version is published to the private repository, run:

```bash
cd /opt/mtg-table-bot
./update.sh
```

The script backs up `.env`, `config.yaml`, `data/`, and `docker-compose.yml`, fetches the latest commit on the current branch, rebuilds the Docker image, restarts the service, and checks that the container is running. If an update step fails, it attempts to restore the previous source commit and rebuild. The backup remains available for manual recovery.

## 5. Check status and logs

```bash
docker compose ps
docker compose logs --tail=100
```

## Important safety notes

- Keep the GitHub repository **Private**.
- Never commit `.env`, `config.yaml`, or `data/`.
- Never run `docker compose down -v` for routine updates.
- Do not delete the `data/` directory; it contains persistent bot data.
- GitHub is a source-code update channel, not an automatic deployment. A person still runs `./update.sh` on the server unless a separate deployment workflow is configured.
