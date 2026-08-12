# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

WebTerm is a Zabbix frontend module that provides an integrated web terminal for SSH/Telnet connections. It embeds xterm.js terminals directly in the Zabbix UI, allowing administrators to connect to monitored hosts without leaving the Zabbix interface.

## Architecture

This is a **Zabbix Frontend Module** (not a standalone application). It follows Zabbix's module architecture:

- **Manifest-driven**: `manifest.json` defines module metadata, actions, and assets
- **Action-based routing**: Actions are PHP classes extending `CController`
- **Zabbix integration**: Module extends `CModule` and hooks into Zabbix's host context menus

### Key Files

| File | Purpose |
|------|---------|
| `manifest.json` | Module definition, action routing, asset registration |
| `Module.php` | Main module class, provides name/description |
| `actions/Connect.php` | Renders the dedicated terminal page |
| `actions/HostInfo.php` | AJAX endpoint returning host IP/interface info |
| `views/connect.view.php` | Standalone terminal page with embedded xterm.js |
| `assets/js/class.webterm.js` | Floating terminal UI that patches Zabbix host menus |
| `assets/css/styles.css` | Terminal window styling |

### Dual Interface Pattern

The module provides two ways to access the terminal:

1. **Dedicated Page** (`connect.view.php`): Full-page terminal accessed via `zabbix.php?action=webterm.connect&hostid=X`
2. **Floating Terminal** (`class.webterm.js`): Popup window with tabs (SecureCRT-style) that monkey-patches `getMenuPopupHost()` to add "Connect > SSH/Telnet" options to host context menus

## External Dependency: WebSocket Proxy

**Critical**: This module requires the WebSocket-to-SSH/Telnet proxy service.
For Zabbix in Docker, run it as the `webterm-proxy` sidecar and expose it
through the same Zabbix HTTPS origin at `/webterm/ws`. Port **8765** should stay
internal to the Docker network.

```javascript
// WebSocket connection (from class.webterm.js)
const WS_URL = buildWebSocketUrl(); // ws(s)://current-host/webterm/ws
```

The proxy implementation and Docker artifacts are in `proxy/`. Without the sidecar or Nginx WebSocket upgrade route, terminals will show "Error de conexión WebSocket".

### Web Mode: Known Root Paths Must Stay in Sync

Device UIs (Cisco `/webui/`, Aruba `/html/`, Mikrotik `/webfig/` + `/jsproxy/`, etc.) escape the `/webterm/web/<token>/` prefix with root-relative URLs. The list of recoverable root paths exists in **4 places that must be updated together**:

1. `proxy/web_http_proxy.py` → `_JS_DIR` (feeds `KNOWN_ROOT_PATH_RE` and `JS_ROOT_PATH`)
2. `config/webterm-locations.conf` (zabbix-coolify repo; mounted into `zabbix-web` nginx)
3. `config/nginx-server-common.conf` (reference copy)
4. `assets/js/class.webterm.js` → `isKnownWebRootPath`

Note: `config/webterm-locations.conf` is a single-file bind mount (inode-based). Edit it **in place** (never replace/rename the file) and recreate `zabbix-web` (`docker compose up -d --force-recreate zabbix-web`) for nginx to see the new content — a plain `nginx -s reload` is not enough if the inode changed.

## Module Configuration

### Enabling the Module

1. Copy this directory to `/usr/share/zabbix/ui/modules/webterm/`
2. In Zabbix frontend: Administration → Modules → Scan directory
3. Enable "WebTerm - Terminal Web"

### Permission Requirements

Both actions require `USER_TYPE_ZABBIX_ADMIN` (Super Admin or Admin). See `checkPermissions()` in action files.

## Development Notes

### Adding Menu Items to Host Context Menu

The floating terminal (`class.webterm.js`) patches the global `getMenuPopupHost()` function:

```javascript
const _orig = getMenuPopupHost;
getMenuPopupHost = function(options, trigger_element) {
    const sections = _orig(options, trigger_element);
    if (options.hostid) {
        sections.push({
            label: t('Connect'),
            items: [
                { label: 'SSH', clickCallback: () => openConnectForm(options.hostid, 'ssh') },
                { label: 'Telnet', clickCallback: () => openConnectForm(options.hostid, 'telnet') }
            ]
        });
    }
    return sections;
};
```

### Host IP Resolution Logic

Both `Connect.php` and `HostInfo.php` use the same IP resolution priority:
1. Main interface (interface with `main=1`)
2. First interface's IP (or DNS if IP is empty)

### xterm.js Loading

The module loads xterm.js dynamically from CDN in `class.webterm.js`:

```javascript
const base = 'https://cdn.jsdelivr.net/npm/';
// xterm@5.3.0, xterm-addon-fit@0.8.0, xterm-addon-web-links@0.9.0
```

The standalone view (`connect.view.php`) loads these via static `<script>` tags.

### WebSocket Protocol

Control messages are JSON, terminal data is raw bytes/text:

```javascript
// Client → Proxy (connection request)
{ mode: 'ssh'|'telnet', host: string, port: number, username: string, password: string }

// Client → Proxy (resize)
{ resize: true, cols: number, rows: number }

// Proxy → Client (status)
{ connected: true } | { disconnected: true } | { error: string }
```

## File Structure

```
/usr/share/zabbix/ui/modules/webterm/
├── manifest.json           # Module manifest (actions, assets)
├── Module.php              # CModule subclass
├── actions/
│   ├── Connect.php         # Page action for dedicated terminal
│   └── HostInfo.php        # AJAX action for host info lookup
├── views/
│   └── connect.view.php    # Standalone terminal page view
└── assets/
    ├── css/styles.css      # Terminal window styling
    └── js/class.webterm.js # Floating terminal + menu integration
```
