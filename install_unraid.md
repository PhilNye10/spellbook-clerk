# Unraid deployment

This file is retained for existing Unraid deployments. For a standard Ubuntu Server installation, use `install_ubuntu.md`.

1. Copy the project to an Unraid appdata share.
2. Copy `.env.example` to `.env` and fill in the Discord/Sesh credentials.
3. Copy `config.example.yaml` to `config.yaml`.
4. Build with `docker compose up -d --build`.
5. Keep the `data/` directory persistent so the SQLite cache survives container restarts.
6. For Sesh Pro webhooks, expose port 8080 through an HTTPS reverse proxy or Cloudflare Tunnel and use `https://YOUR_HOST/webhooks/sesh` in Sesh.
