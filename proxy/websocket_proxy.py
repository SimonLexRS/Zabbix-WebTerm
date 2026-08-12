#!/usr/bin/env python3
"""
WebTerm WebSocket Proxy - Persistent Session Support
Provides persistent SSH/Telnet connections via WebSocket.

Protocol:
- Client -> Proxy: {"mode": "ssh", "host": "...", "port": 22, "username": "...", "password": "..."}
- Client -> Proxy: {"reattach": true, "session_token": "..."}
- Client -> Proxy: {"resize": true, "cols": 80, "rows": 24}
- Client -> Proxy: raw terminal data (string)

- Proxy -> Client: {"connected": true, "session_token": "..."}
- Proxy -> Client: {"error": "...", "reattach_failed": true}
- Proxy -> Client: raw terminal data (string)
"""

import asyncio
import ipaddress
import json
import logging
import re
import signal
import socket
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import websockets
from websockets.server import WebSocketServerProtocol

from session_manager import SessionManager
from ssh_client import SSHClientHandler
from telnet_client import TelnetClientHandler
from web_http_proxy import WebReverseProxy

# Default configuration
DEFAULT_CONFIG = {
    'websocket': {
        'host': '0.0.0.0',
        'port': 8765
    },
    'http': {
        'host': '127.0.0.1',
        'port': 8766
    },
    'web': {
        'public_path': '/webterm/web',
        'public_base_domain': '',
        'public_scheme': 'https',
        'verify_tls': False,
        'timeout': 30,
        'allowed_ports': [],
        'fallback_ports': [80, 443, 4343, 8080, 8443],
        'max_body_size': 25 * 1024 * 1024
    },
    'session': {
        'timeout': 3600,  # 1 hour
        'max_buffer_lines': 10000,
        'cleanup_interval': 300,  # 5 minutes
        'max_sessions': 100
    },
    'logging': {
        'level': 'INFO',
        'file': '/var/log/webterm-proxy.log'
    }
}


class WebTermProxy:
    """WebSocket proxy for persistent terminal sessions."""

    def __init__(self, config: dict):
        self.config = config
        self.session_manager = SessionManager(config)
        self.server = None
        self.web_proxy = WebReverseProxy(config, self.session_manager)
        self._shutdown_event = asyncio.Event()

        # Setup logging
        self._setup_logging()

    def _setup_logging(self):
        """Setup logging configuration."""
        log_config = self.config.get('logging', {})
        level = getattr(logging, log_config.get('level', 'INFO'))
        log_file = log_config.get('file')

        handlers = [logging.StreamHandler(sys.stdout)]
        if log_file:
            try:
                Path(log_file).parent.mkdir(parents=True, exist_ok=True)
                handlers.append(logging.FileHandler(log_file))
            except Exception as e:
                print(f"Warning: Could not create log file: {e}")

        logging.basicConfig(
            level=level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=handlers
        )

        self.logger = logging.getLogger(__name__)

    async def start(self):
        """Start the WebSocket server."""
        await self.session_manager.start()
        await self.web_proxy.start()

        host = self.config['websocket']['host']
        port = self.config['websocket']['port']

        self.server = await websockets.serve(
            self.handle_client,
            host,
            port,
            ping_interval=30,
            ping_timeout=60
        )

        self.logger.info(f"WebTerm proxy started on ws://{host}:{port}")
        self.logger.info("Blocked targets: loopback, link-local, multicast, cloud metadata")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

    async def stop(self):
        """Stop the WebSocket server."""
        self.logger.info("Shutting down WebTerm proxy...")

        if self.server:
            self.server.close()
            await self.server.wait_closed()

        await self.session_manager.stop()
        await self.web_proxy.stop()
        self._shutdown_event.set()
        self.logger.info("WebTerm proxy stopped")

    async def handle_client(self, websocket: WebSocketServerProtocol, path: str):
        """Handle WebSocket client connections."""
        client_addr = websocket.remote_address
        if not self._origin_allowed(websocket):
            self.logger.warning(f"Rejected WebSocket origin from {client_addr}")
            await websocket.close(1008, 'Origin not allowed')
            return

        self.logger.info(f"Client connected: {client_addr}")

        session = None

        try:
            async for message in websocket:
                try:
                    # Try to parse as JSON control message
                    if isinstance(message, bytes):
                        message = message.decode('utf-8', errors='replace')

                    # First character check for JSON
                    if message.strip().startswith('{'):
                        try:
                            data = json.loads(message)

                            # Handle reattachment
                            if data.get('reattach') and data.get('session_token'):
                                session = await self._handle_reattach(websocket, data)
                                continue

                            # Handle new connection
                            if 'mode' in data and 'host' in data:
                                session = await self._handle_new_connection(
                                    websocket, data, session
                                )
                                continue

                            # Handle resize
                            if data.get('resize'):
                                await self._handle_resize(session, data)
                                continue

                        except json.JSONDecodeError:
                            # Not JSON, treat as terminal data
                            pass

                    # Terminal data - forward to session
                    if session and session.connected:
                        await session.write_input(message)

                except Exception as e:
                    self.logger.error(f"Error handling message: {e}")
                    try:
                        await websocket.send(json.dumps({
                            'error': 'Server error'
                        }))
                    except:
                        pass

        except websockets.exceptions.ConnectionClosed:
            self.logger.info(f"Client disconnected: {client_addr}")
        except Exception as e:
            self.logger.error(f"Client handler error: {e}")
        finally:
            # Detach client from session
            if session:
                await session.detach_client(websocket)

                # Don't close the session - keep it running for reattachment!
                # The session will be cleaned up by timeout if no clients reconnect

            self.logger.info(f"Client handler ended: {client_addr}")

    async def _handle_reattach(self, websocket: WebSocketServerProtocol, data: dict):
        """Handle session reattachment."""
        session_token = data.get('session_token')
        session = await self.session_manager.get_session(session_token)

        if session and (session.connected or session.connecting):
            # VALIDACIÓN CRÍTICA: Verificar que el canal SSH sigue vivo
            if session.mode == 'ssh' and session.ssh_shell:
                if session.ssh_shell.closed:
                    self.logger.warning(f"SSH session {session_token} has closed channel, cannot reattach")
                    await websocket.send(json.dumps({
                        'error': 'SSH session closed by server',
                        'reattach_failed': True
                    }))
                    # Limpiar sesión muerta
                    await self.session_manager.remove_session(session_token)
                    return None

            # Attach client to existing session
            await session.attach_client(websocket)

            response = {
                'connected': True,
                'session_token': session.session_id,
                'reattached': True,
                'mode': session.mode,
                'host': session.host,
                'port': session.port
            }
            if session.mode == 'web':
                response.update({
                    'web_url': self._build_web_public_url(session, websocket),
                    'target_url': session.web_target_base
                })
            await websocket.send(json.dumps(response))

            self.logger.info(f"Client {websocket.remote_address} reattached to session {session_token}")
            return session
        else:
            # Session not found or expired
            await websocket.send(json.dumps({
                'error': 'Session not found or expired',
                'reattach_failed': True
            }))
            return None

            self.logger.warning(f"Reattach failed for session {session_token}")

    async def _handle_new_connection(self, websocket: WebSocketServerProtocol,
                                   data: dict, existing_session) -> Optional[object]:
        """Handle new SSH/Telnet connection."""
        mode = data.get('mode')
        host = data.get('host')
        default_port = 22 if mode == 'ssh' else (23 if mode == 'telnet' else 80)
        try:
            port = int(data.get('port', default_port))
        except (TypeError, ValueError):
            port = default_port
        username = data.get('username')
        password = data.get('password')

        # Close existing session if any
        if existing_session:
            await existing_session.detach_client(websocket)

        try:
            if not host or not isinstance(host, str):
                raise Exception('Target host is not allowed')
            if port < 1 or port > 65535:
                raise Exception('Target port is not allowed')
            if self._is_blocked_target(host):
                raise Exception('Target host is not allowed')

            # Create new session
            session = await self.session_manager.create_session(mode, host, port, username)
            await session.attach_client(websocket)
            session.connecting = True

            # Establish protocol connection
            if mode == 'ssh':
                if not username or not password:
                    raise Exception("Username and password required for SSH")

                handler = SSHClientHandler(session)
                await handler.connect(host, port, username, password)

            elif mode == 'telnet':
                handler = TelnetClientHandler(session)
                await handler.connect(host, port)

            elif mode == 'web':
                protocol = data.get('protocol') or ('https' if port == 443 else 'http')
                if protocol not in ('http', 'https'):
                    raise Exception('Invalid web protocol')
                if not self.web_proxy._is_allowed_port(port):
                    raise Exception('Target port is not allowed')

                session.web_protocol = protocol
                session.web_target_base = self._build_web_target_base(protocol, host, port)
                session.connected = True
                session.connecting = False

            else:
                raise Exception(f"Unknown mode: {mode}")

            # Report success only after the protocol session is actually open.
            response = {
                'connected': True,
                'session_token': session.session_id,
                'reattached': False
            }
            if mode == 'web':
                response.update({
                    'mode': 'web',
                    'web_url': self._build_web_public_url(session, websocket),
                    'target_url': session.web_target_base
                })
            await websocket.send(json.dumps(response))

            return session

        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            try:
                await websocket.send(json.dumps({
                    'error': self._client_error(e)
                }))
            except:
                pass

            # Clean up failed session
            if 'session' in locals():
                await self.session_manager.remove_session(session.session_id)

            return None

    def _build_web_target_base(self, protocol: str, host: str, port: int) -> str:
        default_port = 443 if protocol == 'https' else 80
        port_part = '' if int(port) == default_port else f':{port}'
        return f'{protocol}://{host}{port_part}/'

    def _build_web_public_url(self, session, websocket) -> str:
        """Public URL the browser loads in the iframe for a Web session.

        Preferred form is a per-session subdomain origin
        (https://<web_subid>.<base-domain>/) so the device's root-relative URLs
        resolve to the proxy without any HTML rewriting. Falls back to the legacy
        path prefix when no base domain can be determined or the session has no
        DNS-safe label.
        """
        subid = getattr(session, 'web_subid', None)
        scheme, base_domain = self._web_public_origin(websocket)
        if subid and base_domain:
            return f'{scheme}://{subid}.{base_domain}/'

        public_path = self.config.get('web', {}).get('public_path', '/webterm/web').rstrip('/')
        return f'{public_path}/{session.session_id}/'

    def _web_public_origin(self, websocket):
        """Resolve (scheme, base_domain) for Web-mode subdomains.

        Subdomain mode is opt-in: only an explicit web.public_base_domain enables
        it (requires wildcard DNS + cert). Auto-deriving from the WebSocket Host
        would break path-prefix deployments on a plain FQDN. Scheme may still come
        from config or X-Forwarded-Proto; without a configured base domain the
        caller falls back to /webterm/web/<token>/.
        """
        web_cfg = self.config.get('web', {})
        base_domain = (web_cfg.get('public_base_domain') or '').strip().strip('.')
        scheme = (web_cfg.get('public_scheme') or '').strip()

        if not scheme:
            try:
                headers = getattr(websocket, 'request_headers', None)
                if headers is not None:
                    scheme = (headers.get('X-Forwarded-Proto', '') or '').split(',')[0].strip()
            except Exception:
                pass

        if not self._is_domain_name(base_domain):
            base_domain = ''
        if not scheme:
            scheme = 'https'
        return scheme, base_domain

    @staticmethod
    def _is_domain_name(value: str) -> bool:
        """True only for a real DNS name (has a dot, a letter, and is not an IP)."""
        if not value or '.' not in value:
            return False
        if not re.search(r'[a-zA-Z]', value):
            return False  # pure IPv4 / numeric
        if ':' in value:
            return False  # IPv6 literal
        return True

    def _origin_allowed(self, websocket) -> bool:
        """Require a browser Origin that matches Host / X-Forwarded-Host."""
        try:
            headers = getattr(websocket, 'request_headers', None)
            if headers is None:
                return False
            origin = (headers.get('Origin') or '').strip()
            if not origin:
                return False
            origin_host = (urlparse(origin).hostname or '').lower()
            if not origin_host:
                return False
            host = (headers.get('Host') or '').split(':')[0].strip().lower()
            forwarded = (headers.get('X-Forwarded-Host') or '').split(',')[0].strip()
            forwarded_host = forwarded.split(':')[0].lower()
            return origin_host == host or (bool(forwarded_host) and origin_host == forwarded_host)
        except Exception:
            return False

    @staticmethod
    def _ip_blocked(ip) -> bool:
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return True
        if ip in (
            ipaddress.ip_address('169.254.169.254'),
            ipaddress.ip_address('fd00:ec2::254'),
        ):
            return True
        return False

    def _is_blocked_target(self, host: str) -> bool:
        """Block loopback, link-local and cloud metadata. RFC1918 stays allowed."""
        raw = (host or '').strip().strip('[]').lower()
        if not raw:
            return True
        blocked_names = {
            'localhost',
            'localhost.localdomain',
            'ip6-localhost',
            'ip6-loopback',
            'metadata.google.internal',
            'metadata.google.com',
        }
        if raw in blocked_names:
            return True
        try:
            return self._ip_blocked(ipaddress.ip_address(raw))
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(raw, None)
        except socket.gaierror:
            return True
        if not infos:
            return True
        for info in infos:
            try:
                if self._ip_blocked(ipaddress.ip_address(info[4][0])):
                    return True
            except (ValueError, IndexError):
                continue
        return False

    @staticmethod
    def _client_error(exc: Exception) -> str:
        msg = str(exc)
        allowed = (
            'Username and password required for SSH',
            'Invalid web protocol',
            'Target port is not allowed',
            'Target host is not allowed',
            'Authentication failed',
            'Unknown mode',
        )
        for prefix in allowed:
            if msg == prefix or msg.startswith(prefix):
                return prefix
        return 'Connection failed'

    async def _handle_resize(self, session, data: dict):
        """Handle terminal resize."""
        if session:
            cols = int(data.get('cols', 80))
            rows = int(data.get('rows', 24))
            await session.resize(cols, rows)


def load_config(config_path: str = None) -> dict:
    """Load configuration from YAML file or return defaults."""
    import yaml

    config = DEFAULT_CONFIG.copy()

    if config_path and Path(config_path).exists():
        try:
            with open(config_path, 'r') as f:
                user_config = yaml.safe_load(f)
                if user_config:
                    # Deep merge
                    for key, value in user_config.items():
                        if key in config and isinstance(config[key], dict):
                            config[key].update(value)
                        else:
                            config[key] = value
        except Exception as e:
            print(f"Warning: Could not load config: {e}")

    return config


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='WebTerm WebSocket Proxy')
    parser.add_argument('-c', '--config', help='Path to config file')
    parser.add_argument('-H', '--host', help='WebSocket host')
    parser.add_argument('-p', '--port', type=int, help='WebSocket port')
    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)

    # Override with command line arguments
    if args.host:
        config['websocket']['host'] = args.host
    if args.port:
        config['websocket']['port'] = args.port

    # Create and start proxy
    proxy = WebTermProxy(config)

    # Setup signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(proxy.stop()))

    try:
        await proxy.start()
    except Exception as e:
        logging.error(f"Failed to start proxy: {e}")
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
