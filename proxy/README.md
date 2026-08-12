# WebTerm WebSocket Proxy

WebSocket-to-SSH/Telnet proxy with persistent session support for WebTerm.

## Features

- **Persistent Sessions**: Sessions survive page reloads via session tokens
- **Output Buffering**: Terminal history is preserved and replayed on reconnection
- **Dual Protocol**: Supports SSH (via paramiko) and Telnet (via telnetlib3)
- **Web Mode Reverse Proxy**: Opens HTTP/HTTPS device interfaces from the Zabbix proxy host and exposes them under the Zabbix origin.
  Soft-404s that return login HTML for missing CSS/JS/fonts are detected and converted to a proper 404 with the expected MIME type (avoids browser MIME/OTS errors on Cisco Catalyst webui).
- **Concurrent Connections**: Multiple clients can attach to the same session
- **Auto-Cleanup**: Expired sessions are automatically cleaned up

## Installation

### Docker Compose (Recommended for Zabbix in Docker)

Use the proxy as an internal sidecar and publish it through the same Zabbix HTTPS
origin with Nginx:

```bash
docker compose -f docker-compose.yml -f docker-compose.webterm.yml up -d --build webterm-proxy
docker compose exec nginx getent hosts webterm-proxy
```

The service exposes the terminal WebSocket on `8765` and the Web-mode HTTP
reverse proxy on `8766`. The browser should reach them only through Zabbix:
`wss://<zabbix-host>/webterm/ws` and `https://<zabbix-host>/webterm/web/...`,
with Nginx proxying those paths to the internal WebTerm proxy.

Docker uses `config.docker.yaml`, which logs to stdout/stderr instead of
`/var/log/webterm-proxy.log`.

### 1. Install Dependencies

```bash
cd /usr/share/zabbix/ui/modules/webterm/proxy
pip install -r requirements.txt
```

### 2. Run the Proxy

#### Manual (Development)

```bash
python3 websocket_proxy.py
```

With custom config:
```bash
python3 websocket_proxy.py -c config.yaml
```

With custom host/port:
```bash
python3 websocket_proxy.py -H 0.0.0.0 -p 8765
```

#### Systemd (Bare Metal Production)

```bash
# Copy service file
sudo cp webterm-proxy.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable and start
sudo systemctl enable webterm-proxy
sudo systemctl start webterm-proxy

# Check status
sudo systemctl status webterm-proxy
```

### 3. Configure Firewall

For Docker Compose, do not publish `8765`; keep it internal and use Nginx
`/webterm/ws`.

For bare metal direct access, allow port 8765 only when you intentionally expose
the proxy:

```bash
# UFW
sudo ufw allow 8765/tcp

# Firewalld
sudo firewall-cmd --permanent --add-port=8765/tcp
sudo firewall-cmd --reload

# iptables
sudo iptables -I INPUT -p tcp --dport 8765 -j ACCEPT
```

## Configuration

Edit `config.yaml`:

```yaml
websocket:
  host: 0.0.0.0      # Bind address for SSH/Telnet WebSocket
  port: 8765

http:
  host: 127.0.0.1    # Bind address for Web-mode reverse proxy
  port: 8766

web:
  public_path: /webterm/web
  verify_tls: false  # Network equipment often uses self-signed certs
  timeout: 30
  allowed_ports: []  # Empty means any TCP port; set [80,443] to restrict

session:
  timeout: 3600      # Session timeout (1 hour)
  max_buffer_lines: 10000
  cleanup_interval: 300
  max_sessions: 100  # Max concurrent sessions

logging:
  level: INFO
  file: /var/log/webterm-proxy.log
```

## Protocol

### New Connection
```json
{"mode": "ssh", "host": "srv1", "port": 22, "username": "admin", "password": "secret"}
```

Response:
```json
{"connected": true, "session_token": "abc123...", "reattached": false}
```

### Reattach to Session
```json
{"reattach": true, "session_token": "abc123..."}
```

Response (success):
```json
{"connected": true, "session_token": "abc123...", "reattached": true}
```

Response (failure):
```json
{"error": "Session not found or expired", "reattach_failed": true}
```


### Web Mode Connection
```json
{"mode": "web", "host": "192.0.2.10", "port": 443, "protocol": "https"}
```

Response:
```json
{"connected": true, "mode": "web", "session_token": "abc123...", "web_url": "/webterm/web/abc123.../", "target_url": "https://192.0.2.10/"}
```

The iframe must load `web_url`, not the device IP directly. Nginx should proxy
`/webterm/web/` to the HTTP listener (`127.0.0.1:8766` in the host-network
Docker deployment used by this project).

### Resize Terminal
```json
{"resize": true, "cols": 120, "rows": 40}
```

### Terminal Data
Raw strings are forwarded to the SSH/Telnet session.

## Session Flow

1. Client connects via WebSocket
2. Server sends control messages as JSON
3. Terminal data flows as raw strings
4. On disconnect: session keeps running, output is buffered
5. On reattach: buffered output is replayed to the client

## Security Considerations

1. **Session Tokens**: 32-byte URL-safe tokens generated with `secrets.token_urlsafe()`
2. **No Credential Storage**: The proxy only forwards credentials to SSH/Telnet servers
3. **Timeout**: Sessions expire after 1 hour of inactivity
4. **Buffer Limit**: Output buffer limited to 10,000 lines per session
5. **Max Sessions**: Limited to 100 concurrent sessions by default

## Troubleshooting

### Check logs
```bash
sudo journalctl -u webterm-proxy -f
# or
sudo tail -f /var/log/webterm-proxy.log
```

### Test connection
```bash
# Install websocat
pip install websocat

# Connect
websocat ws://localhost:8765

# Docker/HTTPS through Zabbix/Nginx
websocat wss://ZABBIX_FQDN/webterm/ws
```

### Common Issues

**Permission denied for log file:**
```bash
sudo touch /var/log/webterm-proxy.log
sudo chown www-data:www-data /var/log/webterm-proxy.log
```

**Port already in use:**
```bash
sudo lsof -i :8765
sudo kill -9 <PID>
```


**Web interfaces do not load:**
```nginx
location ^~ /webterm/web {
    proxy_pass http://127.0.0.1:8766;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
}

# Some network UIs escape the iframe with root-relative AJAX/assets
# (Cisco IOS-XE /webui/+/restconf/, Aruba /html/, AireOS /screens/,
# Mikrotik /webfig/+/jsproxy/, etc.).
# Keep these requests on the Zabbix origin and let WebTerm recover the token
# from the Referer header. Place this before generic static asset locations.
# try_files first: paths that exist in the Zabbix webroot (e.g. its own
# /assets/) are served directly; only unknown paths reach the WebTerm proxy.
location ~ ^/(?:html/|jscripts/|styles/|images/|i18n/|help/|fonts/|screens/|skins/|skin/|assets/|common/|lib/|features/|cgi-bin/|monitoring/|api/|webui/|restconf/|webfig(?:/|$)|jsproxy(?:/|$)|graphs(?:/|$)|files/|localizationdir$|modern_ui_installed$|helpdir$|swarm\.cgi$|styles\.css$|login$|logincheck$|logout$) {
    expires 14d;
    try_files $uri @webterm_escaped;
}

location @webterm_escaped {
    proxy_pass http://127.0.0.1:8766;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
}
```

Restart/rebuild `webterm-proxy` after code changes. After changing the
escaped-root routing in `config/webterm-locations.conf`, recreate the Zabbix
Nginx container (`docker compose up -d --force-recreate zabbix-web`): the file
is a single-file bind mount, so replacing it changes the inode and a plain
`nginx -s reload` keeps serving the old content.
