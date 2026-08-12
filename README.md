# WebTerm — Terminal web para Zabbix



Módulo de frontend Zabbix que abre sesiones **SSH**, **Telnet** y **Web** (UI del equipo) sin salir de la interfaz. Incluye una ventana flotante con pestañas (estilo SecureCRT) y una página dedicada.

**Creado por [Simon Alex Rodriguez Saavedra](https://www.linkedin.com/in/srodriguezxs/)** — Elitech Solutions.

Repositorio: [https://github.com/SimonLexRS/Zabbix-WebTerm](https://github.com/SimonLexRS/Zabbix-WebTerm)

![GitHub stars](https://img.shields.io/github/stars/SimonLexRS/Zabbix-WebTerm?style=social) ![GitHub forks](https://img.shields.io/github/forks/SimonLexRS/Zabbix-WebTerm?style=social) ![GitHub watchers](https://img.shields.io/github/watchers/SimonLexRS/Zabbix-WebTerm?style=social) ![Visitas](https://hits.seeyoufarm.com/api/count/incr/badge.svg?url=https%3A%2F%2Fgithub.com%2FSimonLexRS%2FZabbix-WebTerm&count_bg=%2379C83D&title_bg=%23555555&icon=&icon_color=%23E7E7E7&title=vistas&edge_flat=false)

> 📊 Las estadísticas detalladas de **vistas** y **clones/descargas** están disponibles para el propietario en [GitHub Insights → Traffic](https://github.com/SimonLexRS/Zabbix-WebTerm/graphs/traffic) (GitHub no expone esos datos públicamente por privacidad). Los badges de arriba (stars, forks, watchers, visitas de README) sí son públicos y se actualizan en tiempo real.


## Requisitos

| Componente | Mínimo | Notas |
|---|---|---|
| **Zabbix Frontend** | **6.4** | Recomendado 7.0 o superior. Usa `manifest_version: 2.0` y controladores con CSRF. |
| PHP | 8.0+ | El que traiga su paquete/imagen de Zabbix. |
| Permisos Zabbix | Admin o Super Admin | Usuarios User/Guest no pueden conectar. |
| Python (proxy) | 3.8+ | Ver `proxy/requirements.txt`. |
| Nginx | con `proxy_pass` y Upgrade WebSocket | El de `zabbix-web` en Docker o el del host. |

Dependencias del proxy: `websockets`, `paramiko`, `telnetlib3`, `pyyaml`, `aiohttp`, `cryptography`.

xterm.js se carga desde CDN (jsDelivr). El navegador necesita acceso a Internet la primera vez, o sustituya las URLs por copias locales.

## Arquitectura

```
Navegador (Zabbix UI)
    |  wss://<zabbix>/webterm/ws
    v
Nginx (mismo origen HTTPS)
    |  interno :8765  (terminal)
    |  interno :8766  (modo Web)
    v
webterm-proxy  -->  SSH / Telnet / HTTP del equipo monitorizado
```

**No publique los puertos 8765 ni 8766.** El navegador solo debe hablar con el origen de Zabbix.

## Instalación del módulo

### 1. Copiar el módulo

**Paquetes (RHEL/Debian):**

```bash
sudo cp -a Zabbix-WebTerm /usr/share/zabbix/ui/modules/webterm
sudo chown -R apache:apache /usr/share/zabbix/ui/modules/webterm   # o www-data
```

**Docker oficial Zabbix (`zabbix-web-nginx-*`):**

Monte el directorio del módulo en `/usr/share/zabbix/modules/webterm` (el frontend escanea esa ruta).

### 2. Activar en Zabbix

1. Inicie sesión como Super Admin.
2. **Administración → Módulos generales → Módulos** (en 7.x: *Administration → General → Modules*).
3. **Scan directory**.
4. Habilite **WebTerm - Terminal Web**.

### 3. Comprobar

En un host, menú contextual → **Connect** → SSH / Telnet / Web.

## Proxy WebSocket (obligatorio)

Sin el proxy, la terminal muestra error de conexión WebSocket.

### Docker Compose (recomendado)

Ejemplo de sidecar. Ajuste la red al stack de Zabbix. **No mapee 8765/8766 al host.**

```yaml
services:
  webterm-proxy:
    build:
      context: ./modules/webterm/proxy
      dockerfile: Dockerfile
    container_name: zabbix-webterm-proxy
    hostname: webterm-proxy
    restart: unless-stopped
    volumes:
      - ./modules/webterm/proxy/config.docker.yaml:/app/config.yaml:ro
    networks:
      - zabbix-network
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import socket; [socket.create_connection(('127.0.0.1', p), 3).close() for p in (8765, 8766)]\""]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

  zabbix-web:
    depends_on:
      - webterm-proxy
    volumes:
      - ./modules:/usr/share/zabbix/modules
      - ./modules/webterm/deploy/nginx-webterm.conf:/etc/nginx/includes/webterm-locations.conf:ro
```

El `server { }` de Nginx de Zabbix debe `include` ese archivo **antes** de las locations genéricas de estáticos.

Tras cambiar un bind-mount de un solo archivo, recree el contenedor:

```bash
docker compose up -d --force-recreate zabbix-web webterm-proxy
```

### Bare metal (systemd)

```bash
cd /usr/share/zabbix/ui/modules/webterm/proxy
sudo pip3 install -r requirements.txt
sudo cp webterm-proxy.service /etc/systemd/system/
# En el unit, cambie las rutas si su frontend no está en /usr/share/zabbix/ui
sudo systemctl daemon-reload
sudo systemctl enable --now webterm-proxy
sudo systemctl status webterm-proxy
```

En `proxy/config.yaml`, deje `http.host: 127.0.0.1`. En `deploy/nginx-webterm.conf` sustituya `webterm-proxy` por `127.0.0.1`.

## Nginx

Archivo de referencia: [`deploy/nginx-webterm.conf`](deploy/nginx-webterm.conf).

Debe existir:

- `/webterm/ws` → proxy WebSocket al puerto **8765** (Upgrade + Connection).
- `/webterm/web` → reverse proxy al puerto **8766** (modo Web).
- El bloque de rutas escapadas (Cisco `/webui/`, Aruba `/html/`, Mikrotik `/webfig/`, etc.) **antes** de locations de `.js`/`.css`.

## Uso

1. En Inventario / Hosts, abra el menú del equipo.
2. **Connect → SSH** (puerto 22), **Telnet** (23) o **Web** (80/443).
3. Introduzca usuario y contraseña (SSH). No se almacenan en disco; el flotante no guarda credenciales en `localStorage`.
4. Puede minimizar, maximizar y abrir varias pestañas.

![WebTerm Screenshot](2026-08-12%2014_52_32-.png)


Página dedicada: `zabbix.php?action=webterm.connect&hostid=<ID>`.

## Seguridad

- Acciones PHP (`webterm.connect`, `webterm.hostinfo`) requieren **Admin o Super Admin**.
- El proxy **no valida la cookie de sesión de Zabbix**. Trátelo como jump-host interno: mismo origen HTTPS, sin publicar 8765/8766.
- El WebSocket exige cabecera `Origin` coincidente con `Host` / `X-Forwarded-Host`.
- Se rechazan destinos loopback, link-local y metadata cloud (`169.254.169.254`). Las redes RFC1918 **sí** están permitidas (es el caso de uso).
- SSH usa `AutoAddPolicy` (equipos de red sin `known_hosts`). Hay riesgo MITM en la LAN; asuma red de gestión de confianza.
- Modo Web: `verify_tls: false` por certificados autofirmados de la electrónica.
- No exponga este módulo a Internet sin VPN o control de acceso adicional.

## Estructura

```
Zabbix-WebTerm/
├── manifest.json
├── Module.php
├── actions/          Connect.php, HostInfo.php
├── views/            connect.view.php
├── assets/           js + css
├── proxy/            WebSocket + reverse proxy HTTP
└── deploy/           nginx-webterm.conf
```

## Licencia y autor

Módulo desarrollado por **Simon Alex Rodriguez Saavedra** (Elitech Solutions).

- LinkedIn: [https://www.linkedin.com/in/srodriguezxs/](https://www.linkedin.com/in/srodriguezxs/)
- GitHub: [https://github.com/SimonLexRS/Zabbix-WebTerm](https://github.com/SimonLexRS/Zabbix-WebTerm)

Powered by Elitech Solutions.
