# Authenticated management MCP service

Auto-Coder can run a management-only Streamable HTTP MCP service independently
of an automation worker. The `auto_coder_whoami` identity tool remains read-only
and idempotent. Additional fixed runtime inspection and single-target
reevaluation tools are described in `management-runtime-mcp.md`. Neither stage
provides arbitrary commands, Docker control, or worker update/restart authority.

Create a persistent directory outside release/beta state and workspaces. Its
`allowed_uids.json` uses the operator-policy format, and the service creates its
durable `oauth.sqlite3` and rotating `logs/management.log` there. A minimal
production configuration is:

```yaml
state_root: /var/lib/auto-coder-management
public_origin: https://management.example.com
firebase_project_id: example-project
bind_host: 127.0.0.1
port: 8081
allowed_browser_origins: [https://console.example.com]
firebase_web_config:
  apiKey: public-firebase-web-key
  authDomain: example-project.firebaseapp.com
  projectId: example-project
clients:
  - client_id: operator-desktop
    redirect_uris: [http://127.0.0.1:49152/callback]
```

Add the public origin to Firebase Authentication's authorized domains and
register each public client's exact redirect URI. Start only this service with
`auto-coder management serve --config /etc/auto-coder/management.yml`. The
canonical OAuth resource is always `<public_origin>/mcp`; incoming Host and
forwarding headers never change it.

Terminate HTTPS at a trusted proxy and keep the default loopback bind private:

```yaml
services:
  management:
    image: ghcr.io/kitamura-tetsuo/auto-coder:latest
    command: [management, serve, --config, /config/management.yml]
    ports: ["443:443"]
    volumes:
      - ./management.yml:/config/management.yml:ro
      - management-state:/var/lib/auto-coder-management
  proxy:
    image: caddy:2
    network_mode: service:management
    depends_on: [management]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile:ro"]
volumes:
  management-state: {}
```

The image already has `auto-coder` as its entrypoint, so the Compose `command`
contains only its arguments. The proxy deliberately shares the management
container's network namespace: Caddy can reach the loopback-only listener, and
only HTTPS port 443 is published. Do not publish port 8081. Use this `Caddyfile`:

```caddyfile
management.example.com {
    reverse_proxy 127.0.0.1:8081
}
```

For explicit local development only, set `public_origin` to a loopback HTTP
origin, `bind_host` to loopback, and `local_development: true`. This profile
still requires OAuth bearer authentication.

An MCP client begins at `/mcp`, follows the 401 protected-resource metadata
challenge, discovers the authorization server, opens the Firebase browser
login, and exchanges the authorization code using PKCE S256. It sends the
resulting bearer token on every initialize, list, call, GET, and DELETE request;
reconnecting does not preserve authorization. A 401 means credentials are
missing or invalid, 403 means scope or current operator policy forbids access,
and 503 means authoritative state cannot be read. Grant revocation, refresh
replay, and operator-policy removal affect the next request and survive service
restart. Firebase account disablement or token revocation is checked on renewal;
issued access tokens expire within 900 seconds.
