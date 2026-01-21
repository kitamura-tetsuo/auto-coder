# Webhook Daemon Mode

Auto-Coder now supports a daemon mode that listens for webhooks from GitHub and Sentry.

## Usage

```bash
auto-coder serve --repo owner/repo --github-webhook-secret <secret> --sentry-webhook-secret <secret>
```

## Exposing Local Port via Cloudflare Tunnel

To expose the local server (default port 8000) to the internet, you can use Cloudflare Tunnel.

1. Install `cloudflared`:
   ```bash
   brew install cloudflare/cloudflare/cloudflared
   ```

2. Start the tunnel:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```

3. Copy the URL provided by cloudflared (e.g., `https://random-name.trycloudflare.com`).

4. Configure Webhooks:
   - **GitHub**: Add a webhook pointing to `https://<your-url>/hooks/github`.
     - Content type: `application/json`
     - Events: Pull requests, Workflow runs
   - **Sentry**: Add a webhook pointing to `https://<your-url>/hooks/sentry`.
