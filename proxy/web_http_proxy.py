"""HTTP/HTTPS reverse proxy for WebTerm web sessions.

The browser only talks to the Zabbix origin. This module forwards requests from
/webterm/web/<session-token>/... to the target network device from the WebTerm
proxy host.
"""

import asyncio
import json
import logging
import re
from html import escape
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from aiohttp import ClientConnectorError, ClientSession, CookieJar, TCPConnector, web
from multidict import CIMultiDict


LOGGER = logging.getLogger(__name__)
HOP_BY_HOP_HEADERS = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade'
}
BLOCKED_RESPONSE_HEADERS = HOP_BY_HOP_HEADERS | {
    'content-length', 'content-encoding', 'x-frame-options',
    'content-security-policy', 'content-security-policy-report-only'
    # set-cookie is forwarded after Path/Domain/Secure rewriting (see _rewrite_set_cookie)
}

# Catalyst/IOS soft-404s often return login HTML (200 text/html) for missing
# static files. Serving that as CSS/JS/fonts breaks the browser (MIME / OTS).
STATIC_ASSET_EXTENSIONS = (
    '.css', '.js', '.mjs', '.map',
    '.woff', '.woff2', '.ttf', '.otf', '.eot', '.svg',
)
STATIC_ASSET_MIME = {
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.mjs': 'application/javascript; charset=utf-8',
    '.map': 'application/json',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf': 'font/ttf',
    '.otf': 'font/otf',
    '.eot': 'application/vnd.ms-fontobject',
    '.svg': 'image/svg+xml',
}


class PreferAuthCookieJar(CookieJar):
    """Cookie jar that keeps a good Auth cookie when the device sends Auth=deleted.

    Unauthenticated asset responses from Cisco webui often include
    Set-Cookie: Auth=deleted. Storing that would wipe the session established by
    Basic-auth login and make every subsequent CSS/JS request return login HTML.
    """

    def update_cookies(self, cookies, response_url=None):
        from collections.abc import Mapping
        from http.cookies import SimpleCookie
        from yarl import URL

        if cookies is None:
            return

        pairs = cookies.items() if isinstance(cookies, Mapping) else cookies
        filtered = SimpleCookie()
        for name, cookie in pairs:
            value = getattr(cookie, 'value', cookie)
            if str(name).lower() == 'auth' and str(value).lower() == 'deleted':
                continue
            if hasattr(cookie, 'value'):
                filtered[name] = cookie.value
                for attr_key in cookie.keys():
                    filtered[name][attr_key] = cookie[attr_key]
            else:
                filtered[name] = str(cookie)

        if len(filtered) == 0:
            return
        url = URL() if response_url is None else response_url
        return super().update_cookies(filtered, url)


class WebReverseProxy:
    """A small reverse proxy scoped to WebTerm web sessions."""

    # Directory roots may appear bare ("/webui/") — required so login.js
    # `location.pathname = "/webui/"` is rewritten into the session prefix.
    _JS_DIR = (
        r'html|jscripts|styles|images|i18n|help|fonts|screens|skins|skin|assets|'
        r'common|lib|features|cgi-bin|monitoring|api|webui|restconf|'
        r'webfig|jsproxy|graphs|files'
    )
    KNOWN_ROOT_PATH = (
        r'(?:(?:' + _JS_DIR + r')(?:/|$)|'
        r'localizationdir\b|modern_ui_installed\b|helpdir\b|swarm\.cgi\b|'
        r'styles\.css\b|login\b|logincheck\b|logout\b|favicon\.ico\b)'
    )
    KNOWN_ROOT_PATH_RE = re.compile(r'^/?' + KNOWN_ROOT_PATH, re.I)
    # Optional path after the directory segment so "/webui/" and "/webui/index.html" both match.
    JS_ROOT_PATH = (
        r'(?:(?:' + _JS_DIR + r')(?:/[^"\'<>\\\s]*)?|'
        r'localizationdir\b|modern_ui_installed\b|helpdir\b|swarm\.cgi\b|'
        r'styles\.css\b|login\b|logincheck\b|logout\b|favicon\.ico\b)'
    )

    def __init__(self, config: dict, session_manager):
        self.config = config
        self.session_manager = session_manager
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.connector: Optional[TCPConnector] = None
        self.logger = logging.getLogger(__name__)

    async def start(self):
        web_config = self.config.get('http', {})
        host = web_config.get('host', '127.0.0.1')
        port = int(web_config.get('port', 8766))

        verify_tls = self.config.get('web', {}).get('verify_tls', False)
        timeout = self.config.get('web', {}).get('timeout', 30)
        self.connector = TCPConnector(ssl=None if verify_tls else False)

        app = web.Application(client_max_size=self.config.get('web', {}).get('max_body_size', 25 * 1024 * 1024))
        app.router.add_get('/healthz', self.healthz)
        # Legacy path-prefix routes (kept for backward compatibility).
        app.router.add_route('*', '/webterm/web/{token}/{tail:.*}', self.handle)
        app.router.add_route('*', '/webterm/web/{token}', self.handle)
        # Catch-all: subdomain mode (session resolved from the Host label) and
        # legacy escaped-path recovery. Matches every path including '/'.
        app.router.add_route('*', '/{tail:.*}', self.handle)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        self.logger.info('WebTerm HTTP reverse proxy started on http://%s:%s', host, port)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
        if self.connector:
            await self.connector.close()
        self.runner = None
        self.site = None
        self.connector = None

    @staticmethod
    def _build_timeout(seconds):
        from aiohttp import ClientTimeout
        return ClientTimeout(total=float(seconds))

    async def healthz(self, request: web.Request) -> web.Response:
        return web.Response(text='ok')

    async def handle(self, request: web.Request) -> web.StreamResponse:
        # Subdomain mode: the session is identified by the Host label, and the
        # request path is the device's real path (no proxy prefix to strip).
        session = await self._session_from_host(request)
        if session is not None:
            return await self._proxy_to_device(request, session, request.path.lstrip('/'), subdomain=True)

        # Legacy path mode: /webterm/web/{token}/{tail}
        token = request.match_info.get('token')
        tail = request.match_info.get('tail', '') or ''
        session = await self.session_manager.get_session(token) if token else None

        if not session or session.mode != 'web' or not getattr(session, 'connected', False):
            recovered = await self._recover_missing_token_response(request, token, tail)
            if recovered is not None:
                return recovered
            return self._expired_session_response(token)

        return await self._proxy_to_device(request, session, tail, subdomain=False)

    async def _session_from_host(self, request: web.Request):
        """Resolve a Web session from the request Host's leftmost label."""
        host = request.headers.get('Host', '') or request.host or ''
        label = host.split(':', 1)[0].split('.', 1)[0]
        if not label:
            return None
        session = await self.session_manager.get_web_session_by_subid(label)
        if session and session.mode == 'web' and getattr(session, 'connected', False):
            return session
        return None

    async def _proxy_to_device(self, request: web.Request, session, tail: str, subdomain: bool) -> web.StreamResponse:
        if not subdomain:
            canonical = self._canonical_session_url_response(request, session, tail)
            if canonical is not None:
                return canonical

        if not self._is_allowed_port(session.port):
            return web.Response(status=403, text='Target port is not allowed')

        tail = self._normalize_proxy_path(session, tail)
        query_string = self._normalize_query_string(session, request.query_string)
        target_url = self._target_url(session, tail, query_string)
        session.update_activity()

        body = await request.read()
        headers = self._request_headers(request, session, target_url)

        try:
            client = self._client_for_session(session)
            async with client.request(
                request.method,
                target_url,
                headers=headers,
                data=body if body else None,
                allow_redirects=False
            ) as upstream:
                payload = await upstream.read()
                response_headers = self._response_headers(upstream.headers, session, request)
                status = upstream.status
                content_type = upstream.headers.get('Content-Type', '')

                guarded = await self._guard_html_static_asset(
                    request, session, client, target_url, tail, body,
                    status, content_type, payload, response_headers, subdomain
                )
                if guarded is not None:
                    return guarded

                if self._should_rewrite(content_type):
                    payload = self._rewrite_payload(payload, content_type, session, request, subdomain)

                self._log_suspicious_swarm_response(target_url, status, content_type, payload)
                return web.Response(status=status, headers=response_headers, body=payload)

        except asyncio.TimeoutError:
            return self._error_response(504, 'Tiempo agotado conectando desde Zabbix al equipo web', target_url)
        except ClientConnectorError as exc:
            fallback_response = await self._fallback_after_refused_port(exc, session, request, body, tail, query_string, subdomain)
            if fallback_response is not None:
                return fallback_response
            return self._connector_error_response(exc, target_url, session)
        except Exception as exc:
            self.logger.warning('Web proxy request failed for %s: %s', target_url, exc)
            return self._error_response(502, f'Error del proxy web: {exc}', target_url)

    async def _fallback_after_refused_port(self, exc: ClientConnectorError, session, request: web.Request, body: bytes, tail: str, query_string: str, subdomain: bool = False):
        os_error = getattr(exc, 'os_error', None)
        if getattr(os_error, 'errno', None) != 111:
            return None
        if request.method not in ('GET', 'HEAD') or body or tail:
            return None

        original_protocol = session.web_protocol
        original_host = session.host
        original_port = int(session.port)
        original_base = session.web_target_base

        for protocol, port in self._fallback_targets(original_protocol, original_port):
            self._set_session_target(session, protocol, original_host, port, 'switched by fallback')
            target_url = self._target_url(session, tail, query_string)
            headers = self._request_headers(request, session, target_url)
            self.logger.info(
                'Web session %s fallback after refused port %s:%s -> %s',
                session.session_id, original_host, original_port, target_url
            )
            try:
                client = self._client_for_session(session)
                async with client.request(
                    request.method,
                    target_url,
                    headers=headers,
                    data=None,
                    allow_redirects=False
                ) as upstream:
                    payload = await upstream.read()
                    response_headers = self._response_headers(upstream.headers, session, request)
                    content_type = upstream.headers.get('Content-Type', '')
                    status = upstream.status
                    guarded = await self._guard_html_static_asset(
                        request, session, client, target_url, tail, b'',
                        status, content_type, payload, response_headers, subdomain
                    )
                    if guarded is not None:
                        return guarded
                    if self._should_rewrite(content_type):
                        payload = self._rewrite_payload(payload, content_type, session, request, subdomain)
                    self._log_suspicious_swarm_response(target_url, status, content_type, payload)
                    return web.Response(status=status, headers=response_headers, body=payload)
            except (asyncio.TimeoutError, ClientConnectorError) as fallback_exc:
                self.logger.info('Web fallback target %s failed: %s', target_url, fallback_exc)

        session.web_protocol = original_protocol
        session.host = original_host
        session.port = original_port
        session.web_target_base = original_base
        return None

    def _fallback_targets(self, protocol: str, original_port: int):
        configured = self.config.get('web', {}).get('fallback_ports', [])
        for value in configured:
            try:
                port = int(value)
            except (TypeError, ValueError):
                continue
            if port == int(original_port) or not self._is_allowed_port(port):
                continue
            yield self._protocol_for_port(port, protocol), port

    @staticmethod
    def _protocol_for_port(port: int, default_protocol: str = 'http') -> str:
        if int(port) in (443, 4343, 8443):
            return 'https'
        if int(port) in (80, 8080):
            return 'http'
        return default_protocol if default_protocol in ('http', 'https') else 'http'

    def _client_for_session(self, session) -> ClientSession:
        if session.web_client and not session.web_client.closed:
            return session.web_client
        timeout = self.config.get('web', {}).get('timeout', 30)
        session.web_client = ClientSession(
            connector=self.connector,
            connector_owner=False,
            cookie_jar=PreferAuthCookieJar(unsafe=True),
            timeout=self._build_timeout(timeout)
        )
        return session.web_client

    def _is_allowed_port(self, port: int) -> bool:
        allowed = self.config.get('web', {}).get('allowed_ports', [])
        if not allowed:
            return 1 <= int(port) <= 65535
        return int(port) in {int(p) for p in allowed}

    def _target_url(self, session, tail: str, query_string: str) -> str:
        base = getattr(session, 'web_target_base', '') or f'{session.web_protocol}://{session.host}:{session.port}/'
        if not base.endswith('/'):
            base += '/'
        path = tail or ''
        target = urljoin(base, path)
        if query_string:
            separator = '&' if urlsplit(target).query else '?'
            target = f'{target}{separator}{query_string}'
        return target

    def _normalize_proxy_path(self, session, value: str) -> str:
        return self._strip_embedded_proxy_prefix(session, value or '').lstrip('/')

    def _normalize_query_string(self, session, query_string: str) -> str:
        if not query_string:
            return ''
        pairs = parse_qsl(query_string, keep_blank_values=True)
        normalized = []
        changed = False
        for name, value in pairs:
            new_value = self._strip_embedded_proxy_prefix(session, value)
            changed = changed or new_value != value
            normalized.append((name, new_value))
        return urlencode(normalized, doseq=True) if changed else query_string

    def _strip_embedded_proxy_prefix(self, session, value: str) -> str:
        if not value:
            return value
        token = re.escape(session.session_id)
        return re.sub(r'(^|/)webterm/web/' + token + r'/', r'\1', value)

    def _request_headers(self, request: web.Request, session, target_url: str = '') -> dict:
        headers = {}
        for name, value in request.headers.items():
            lname = name.lower()
            if lname in HOP_BY_HOP_HEADERS or lname == 'host':
                continue
            if lname == 'cookie':
                # Forward browser cookies (Path already rewritten to the session
                # prefix) but drop Auth=deleted so it cannot wipe upstream auth.
                value = self._sanitize_browser_cookie(value)
                if not value:
                    continue
            if lname == 'authorization':
                # Capture Basic from Cisco login XHR so fonts/CSS can reuse it.
                if value and value.lower().startswith('basic '):
                    session.web_basic_auth = value
            if lname == 'referer':
                value = self._upstream_referer(value, session)
            headers[name] = value

        # Replay stored Basic Auth when the browser omits Authorization
        # (typical for @font-face and stylesheet subresources).
        if getattr(session, 'web_basic_auth', None):
            has_auth = any(k.lower() == 'authorization' for k in headers)
            if not has_auth:
                headers['Authorization'] = session.web_basic_auth

        if target_url:
            headers = self._ensure_upstream_auth_cookies(headers, session, target_url)

        host = session.host
        default_port = 443 if session.web_protocol == 'https' else 80
        headers['Host'] = host if int(session.port) == default_port else f'{host}:{session.port}'
        return headers

    @staticmethod
    def _phantom_entrypoint_kind(path: str) -> Optional[str]:
        """Classify Catalyst phantom Angular entrypoints (missing on some IOS images).

        Returns 'js' or 'css', or None if the path is a normal static asset.
        Root stub styles.css only — not assets/styles/*.css.
        """
        clean = (path or '').split('?', 1)[0]
        lower = clean.lower()
        base = lower.rsplit('/', 1)[-1]
        if base in ('runtime.js', 'polyfills.js', 'main.js'):
            return 'js'
        if base == 'styles.css' and 'assets/styles/' not in lower:
            return 'css'
        return None

    @staticmethod
    def _static_asset_extension(path: str) -> Optional[str]:
        """Return a known static extension for soft-404 HTML detection, else None."""
        clean = (path or '').split('?', 1)[0].lower()
        for ext in STATIC_ASSET_EXTENSIONS:
            if clean.endswith(ext):
                if ext == '.svg' and '/fonts/' not in clean and not clean.startswith('fonts/'):
                    return None
                return ext
        return None

    @staticmethod
    def _is_html_payload(content_type: str, payload: bytes) -> bool:
        ctype = (content_type or '').lower()
        if 'text/html' in ctype:
            return True
        head = (payload or b'')[:64].lstrip().lower()
        return head.startswith(b'<!doctype') or head.startswith(b'<html')

    @staticmethod
    def _static_asset_missing_response(ext: str, path: str = '') -> web.Response:
        phantom = WebReverseProxy._phantom_entrypoint_kind(path)
        if phantom == 'js':
            # Valid empty ES module so type=module scripts do not ERR_ABORTED.
            return web.Response(
                status=200,
                body=b'export {};\n',
                headers={'Content-Type': 'application/javascript; charset=utf-8'},
            )
        if phantom == 'css':
            return web.Response(
                status=200,
                body=b'/* missing */\n',
                headers={'Content-Type': 'text/css; charset=utf-8'},
            )
        mime = STATIC_ASSET_MIME.get(ext, 'application/octet-stream')
        if ext == '.css':
            body = b'/* missing */'
        elif ext in ('.js', '.mjs'):
            body = b''
        else:
            body = b''
        return web.Response(status=404, body=body, headers={'Content-Type': mime})

    def _jar_auth_cookie(self, session, target_url: str) -> Optional[str]:
        client = getattr(session, 'web_client', None)
        if not client or client.closed or not target_url:
            return None
        try:
            from yarl import URL
            filtered = client.cookie_jar.filter_cookies(URL(target_url))
            for name, morsel in filtered.items():
                if str(name).lower() == 'auth' and str(morsel.value).lower() != 'deleted':
                    return f'Auth={morsel.value}'
        except Exception:
            return None
        return None

    def _ensure_upstream_auth_cookies(self, headers: dict, session, target_url: str) -> dict:
        """Ensure upstream Cookie includes Auth from the session jar when present."""
        jar_auth = self._jar_auth_cookie(session, target_url)
        if not jar_auth:
            return headers

        cookie_key = None
        cookie_val = None
        for key, value in headers.items():
            if key.lower() == 'cookie':
                cookie_key = key
                cookie_val = value
                break

        parts = []
        has_good_auth = False
        if cookie_val:
            for part in cookie_val.split(';'):
                piece = part.strip()
                if not piece or '=' not in piece:
                    continue
                name, raw = piece.split('=', 1)
                if name.strip().lower() == 'auth':
                    if raw.strip().lower() == 'deleted':
                        continue
                    has_good_auth = True
                    parts.append(piece)
                else:
                    parts.append(piece)
        if not has_good_auth:
            parts.append(jar_auth)

        merged = '; '.join(parts)
        if cookie_key:
            headers[cookie_key] = merged
        else:
            headers['Cookie'] = merged
        return headers

    def _force_credentials(self, headers: dict, session, target_url: str) -> dict:
        """Force Basic + jar Auth for a soft-404 retry on static assets."""
        forced = {k: v for k, v in headers.items() if k.lower() != 'authorization'}
        if getattr(session, 'web_basic_auth', None):
            forced['Authorization'] = session.web_basic_auth
        return self._ensure_upstream_auth_cookies(forced, session, target_url)

    async def _guard_html_static_asset(
        self,
        request: web.Request,
        session,
        client: ClientSession,
        target_url: str,
        tail: str,
        body: bytes,
        status: int,
        content_type: str,
        payload: bytes,
        response_headers,
        subdomain: bool,
    ) -> Optional[web.Response]:
        """Detect login-HTML soft-404s for CSS/JS/fonts; retry once; else 404 with correct MIME."""
        ext = self._static_asset_extension(tail)
        if not ext or not self._is_html_payload(content_type, payload):
            return None

        retried = False
        if request.method in ('GET', 'HEAD') and not body:
            retry_headers = self._force_credentials(
                self._request_headers(request, session, target_url),
                session,
                target_url,
            )
            try:
                async with client.request(
                    request.method,
                    target_url,
                    headers=retry_headers,
                    data=None,
                    allow_redirects=False,
                ) as upstream:
                    retry_payload = await upstream.read()
                    retry_ct = upstream.headers.get('Content-Type', '')
                    retried = True
                    if not self._is_html_payload(retry_ct, retry_payload):
                        response_headers = self._response_headers(upstream.headers, session, request)
                        status = upstream.status
                        content_type = retry_ct
                        payload = retry_payload
                        self.logger.warning(
                            'Web session %s recovered static asset after auth retry path=%s basic_auth=%s',
                            session.session_id, tail, bool(getattr(session, 'web_basic_auth', None)),
                        )
                        if self._should_rewrite(content_type):
                            payload = self._rewrite_payload(
                                payload, content_type, session, request, subdomain
                            )
                        self._log_suspicious_swarm_response(target_url, status, content_type, payload)
                        return web.Response(status=status, headers=response_headers, body=payload)
            except Exception as exc:
                self.logger.info(
                    'Web session %s static-asset auth retry failed path=%s: %s',
                    session.session_id, tail, exc,
                )

        self.logger.warning(
            'Web session %s soft-404 HTML for static asset path=%s basic_auth=%s retry=%s phantom=%s',
            session.session_id,
            tail,
            bool(getattr(session, 'web_basic_auth', None)),
            retried,
            bool(self._phantom_entrypoint_kind(tail)),
        )
        return self._static_asset_missing_response(ext, tail)

    @staticmethod
    def _sanitize_browser_cookie(value: str) -> str:
        if not value:
            return ''
        kept = []
        for part in value.split(';'):
            piece = part.strip()
            if not piece or '=' not in piece:
                continue
            name, raw = piece.split('=', 1)
            if name.strip().lower() == 'auth' and raw.strip().lower() == 'deleted':
                continue
            kept.append(piece)
        return '; '.join(kept)

    def _upstream_referer(self, referer: str, session) -> str:
        try:
            parsed = urlsplit(referer)
            path = parsed.path or '/'
            subid = getattr(session, 'web_subid', None)
            ref_label = (parsed.hostname or '').split('.', 1)[0]
            prefix = self.config.get('web', {}).get('public_path', '/webterm/web').rstrip('/')
            session_prefix = f'{prefix}/{session.session_id}/'
            if subid and ref_label == subid:
                # Subdomain mode: the referer path is already the device path.
                target_path = path.lstrip('/')
            elif path.startswith(session_prefix):
                target_path = path[len(session_prefix):]
            elif self._is_known_root_path(path):
                target_path = path.lstrip('/')
            else:
                return self._target_base(session)
            target = urljoin(self._target_base(session), target_path)
            if parsed.query:
                target += '?' + parsed.query
            return target
        except Exception:
            return self._target_base(session)

    def _response_headers(self, upstream_headers, session, request: web.Request) -> dict:
        # CIMultiDict preserves multiple Set-Cookie headers from the device.
        headers = CIMultiDict()
        for name, value in upstream_headers.items():
            lname = name.lower()
            if lname in BLOCKED_RESPONSE_HEADERS:
                continue
            if lname == 'location':
                headers.add(name, self._rewrite_location(value, session, request))
            elif lname == 'set-cookie':
                # Do not push Auth=deleted to the browser; it would clear the
                # rewritten-Path session cookie and bounce the SPA back to login.
                if re.match(r'(?i)^\s*Auth\s*=\s*deleted\b', value or ''):
                    continue
                headers.add(name, self._rewrite_set_cookie(value, session, request))
            else:
                headers.add(name, value)
        return headers

    def _rewrite_set_cookie(self, value: str, session, request: web.Request) -> str:
        """Rewrite device Set-Cookie so the browser scopes it to this web session.

        Catalyst webui sets Auth with Path=/webui (and often Secure). The iframe URL
        is /webterm/web/<token>/webui/..., so an unre written Path never matches and
        the browser either omits Auth or keeps a prior Auth=deleted with Path=/.
        """
        if not value:
            return value

        proxy_base = self._proxy_base(session, request)
        if re.match(r'^https?://', proxy_base, re.I):
            cookie_path = '/'
        else:
            cookie_path = proxy_base if proxy_base.endswith('/') else proxy_base + '/'

        scheme = (request.headers.get('X-Forwarded-Proto') or request.scheme or 'https')
        scheme = scheme.split(',')[0].strip().lower()
        drop_secure = scheme == 'http'

        parts = [p.strip() for p in value.split(';')]
        if not parts or not parts[0]:
            return value

        rewritten = [parts[0]]
        saw_path = False
        for attr in parts[1:]:
            if not attr:
                continue
            alower = attr.lower()
            if alower.startswith('path='):
                rewritten.append(f'Path={cookie_path}')
                saw_path = True
                continue
            if alower.startswith('domain='):
                continue
            if drop_secure and alower == 'secure':
                continue
            rewritten.append(attr)

        if not saw_path:
            rewritten.append(f'Path={cookie_path}')
        return '; '.join(rewritten)

    def _canonical_session_url_response(self, request: web.Request, session, tail: str) -> Optional[web.Response]:
        if tail or request.path.endswith('/'):
            return None
        proxy_path = self._proxy_base(session, request).rstrip('/')
        if request.path != proxy_path:
            return None
        location = proxy_path + '/'
        if request.query_string:
            location += '?' + request.query_string
        return web.HTTPFound(location=location)

    async def _recover_missing_token_response(self, request: web.Request, token: str, tail: str) -> Optional[web.Response]:
        escaped_path = token or ''
        if tail:
            escaped_path += '/' + tail
        if not self._is_known_root_path(escaped_path):
            return None

        ref_token = self._token_from_referer(request)
        if not ref_token or ref_token == token:
            return None

        session = await self.session_manager.get_session(ref_token)
        if not session or session.mode != 'web' or not getattr(session, 'connected', False):
            return None

        prefix = self.config.get('web', {}).get('public_path', '/webterm/web').rstrip('/')
        location = f'{prefix}/{ref_token}/{escaped_path.lstrip("/")}'
        if request.query_string:
            location += '?' + request.query_string
        self.logger.info('Recovered missing web token path %s using referer session %s', request.path_qs, ref_token)
        return web.HTTPFound(location=location)

    def _token_from_referer(self, request: web.Request) -> Optional[str]:
        referer = request.headers.get('Referer', '')
        if not referer:
            return None
        prefix = self.config.get('web', {}).get('public_path', '/webterm/web').rstrip('/')
        match = re.search(re.escape(prefix) + r'/([^/?#]+)/', referer)
        return match.group(1) if match else None

    def _is_known_root_path(self, path: str) -> bool:
        return bool(self.KNOWN_ROOT_PATH_RE.match(path or ''))

    def _proxy_base(self, session, request: web.Request) -> str:
        # Subdomain mode: the proxy "base" is the per-session origin
        # (https://<subid>.<base>/), so root-relative URLs already point here.
        subid = getattr(session, 'web_subid', None)
        host = request.headers.get('Host', '') or request.host or ''
        label = host.split(':', 1)[0].split('.', 1)[0]
        if subid and label == subid:
            scheme = (request.headers.get('X-Forwarded-Proto') or request.scheme or 'https').split(',')[0].strip()
            return f'{scheme}://{host}/'
        prefix = self.config.get('web', {}).get('public_path', '/webterm/web').rstrip('/')
        return f'{prefix}/{session.session_id}/'

    def _target_base(self, session) -> str:
        default_port = 443 if session.web_protocol == 'https' else 80
        port = '' if int(session.port) == default_port else f':{session.port}'
        return f'{session.web_protocol}://{session.host}{port}/'

    def _rewrite_location(self, location: str, session, request: web.Request) -> str:
        if not location:
            return location

        current_base = self._target_base(session)
        absolute = urljoin(current_base, location)
        target = urlsplit(absolute)

        # Network equipment commonly redirects http/80 or https/443 to a
        # management port, and controller clusters can redirect to a different
        # management IP. Keep the browser on Zabbix and move the session target.
        if target.scheme in ('http', 'https') and target.hostname:
            port = self._effective_port(target)
            same_host = self._is_same_target_host(target.hostname, session.host)
            follow_cross_host = self.config.get('web', {}).get('follow_cross_host_redirects', True)
            if self._is_allowed_port(port) and (same_host or follow_cross_host):
                self._set_session_target(session, target.scheme, target.hostname, port)
                if not same_host:
                    self.logger.info(
                        'Web session %s followed cross-host redirect inside proxy to %s',
                        session.session_id, session.web_target_base
                    )
                return self._absolute_to_proxy_url(absolute, session, request)

        # Avoid sending the iframe directly to non-HTTP targets or blocked ports.
        self.logger.warning('Blocked unsupported redirect for web session %s: %s', session.session_id, location)
        return self._proxy_base(session, request)

    def _absolute_to_proxy_url(self, url: str, session, request: web.Request) -> str:
        target = urlsplit(url)
        proxy_base = self._proxy_base(session, request)
        path = target.path.lstrip('/')
        rewritten = proxy_base + path
        if target.query:
            rewritten += '?' + target.query
        if target.fragment:
            rewritten += '#' + target.fragment
        return rewritten

    @staticmethod
    def _effective_port(parsed_url) -> int:
        if parsed_url.port:
            return int(parsed_url.port)
        return 443 if parsed_url.scheme == 'https' else 80

    @staticmethod
    def _is_same_target_host(host_a: str, host_b: str) -> bool:
        return (host_a or '').strip('[]').lower() == (host_b or '').strip('[]').lower()

    def _set_session_target(self, session, protocol: str, host: str, port: int, reason: str = 'followed same-host redirect'):
        old_base = getattr(session, 'web_target_base', None)
        session.web_protocol = protocol
        session.host = host.strip('[]')
        session.port = int(port)
        session.web_target_base = self._target_base(session)
        if old_base != session.web_target_base:
            self.logger.info('Web session %s %s to %s', session.session_id, reason, session.web_target_base)

    @staticmethod
    def _expired_session_response(token: str) -> web.Response:
        message = {'type': 'webterm-web-session-expired', 'token': token or ''}
        body = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<title>WebTerm Web Proxy</title>'
            '<script>(function(){try{window.parent.postMessage(' + json.dumps(message) + ', window.location.origin);}catch(e){}})();</script>'
            '</head><body style="font-family:sans-serif;background:#111827;color:#e5eef8;padding:24px;">'
            '<h3 style="color:#ffc859;">Sesion web expirada</h3>'
            '<p>Recreando la sesion web desde Zabbix...</p>'
            '</body></html>'
        )
        return web.Response(status=404, text=body, content_type='text/html')

    def _connector_error_response(self, exc: ClientConnectorError, target_url: str, session) -> web.Response:
        os_error = getattr(exc, 'os_error', None)
        errno = getattr(os_error, 'errno', None)
        if errno == 111:
            message = (
                f'Puerto cerrado o conexión rechazada desde Zabbix: '
                f'{session.host}:{session.port}. Verifique puerto/protocolo del equipo.'
            )
        elif errno == 113:
            message = f'No hay ruta desde Zabbix hacia {session.host}:{session.port}.'
        else:
            message = f'No se pudo conectar desde Zabbix a {session.host}:{session.port}: {exc}'

        self.logger.warning('Web proxy connector error for %s: %s', target_url, message)
        return self._error_response(502, message, target_url)

    @staticmethod
    def _error_response(status: int, message: str, target_url: str) -> web.Response:
        body = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<title>WebTerm Web Proxy</title></head>'
            '<body style="font-family:sans-serif;background:#111827;color:#e5eef8;padding:24px;">'
            '<h3 style="color:#e45959;">No se pudo abrir el modo Web</h3>'
            f'<p>{escape(message)}</p>'
            f'<p style="color:#ffc859;word-break:break-all;">Destino: {escape(target_url)}</p>'
            '</body></html>'
        )
        return web.Response(status=status, text=body, content_type='text/html')

    @staticmethod
    def _should_rewrite(content_type: str) -> bool:
        ctype = content_type.lower()
        return any(kind in ctype for kind in ('text/html', 'text/css', 'javascript', 'application/json'))

    def _rewrite_payload(self, payload: bytes, content_type: str, session, request: web.Request, subdomain: bool = False) -> bytes:
        charset = 'utf-8'
        match = re.search(r'charset=([^;]+)', content_type, re.I)
        if match:
            charset = match.group(1).strip()
        try:
            text = payload.decode(charset, errors='replace')
        except LookupError:
            charset = 'utf-8'
            text = payload.decode(charset, errors='replace')

        target_base = self._target_base(session)
        proxy_base = self._proxy_base(session, request)
        doc_base = proxy_base if subdomain else self._document_base(request, proxy_base)
        text = self._rewrite_text(text, target_base, proxy_base, content_type, doc_base, subdomain)
        return text.encode(charset, errors='replace')

    def _document_base(self, request: web.Request, proxy_base: str) -> str:
        # Base href for relative URLs must point at the directory of the current
        # document, not the session root. Devices like the Cisco Catalyst 9800
        # serve their UI under /webui/ and load assets with relative paths; a
        # session-root base would drop the /webui/ segment and 404 on the device.
        path = request.path or proxy_base
        if not path.endswith('/'):
            path = path.rsplit('/', 1)[0] + '/'
        return path if path.startswith(proxy_base) else proxy_base

    def _rewrite_text(self, text: str, target_base: str, proxy_base: str, content_type: str = '', doc_base: str = None, subdomain: bool = False) -> str:
        target_origin = target_base.rstrip('/')
        proxy_prefix = proxy_base.rstrip('/')
        ctype = (content_type or '').lower()
        base_href = doc_base or proxy_base

        # Absolute target URLs first.
        text = text.replace(target_base, proxy_base)
        text = text.replace(target_origin, proxy_prefix)

        # Drop Angular (Ivy) entrypoints that many Catalyst images advertise but
        # do not ship — otherwise <app-root> stays empty (blank UI) while the
        # AngularJS app-bundle still loads in the background.
        if 'text/html' in ctype:
            text = self._strip_phantom_angular_tags(text)

        # Subdomain mode: the per-session origin IS the base, so root-relative
        # URLs (/foo) already resolve to the proxy. No attribute/CSS/JS path
        # rewriting, no <base> injection and no history/fetch/XHR shims needed.
        if subdomain:
            return text

        if 'text/html' in ctype:
            injection = ''
            if '<base ' not in text.lower():
                injection += f'<base href="{escape(base_href, quote=True)}">'
            if '__WEBTERM_PROXY_BASE__' not in text:
                injection += self._history_shim(proxy_base)
            if injection:
                text = re.sub(r'(<head\b[^>]*>)', lambda m: m.group(1) + injection, text, count=1, flags=re.I)

        # Common root-relative HTML attributes and CSS urls.
        attr_pattern = re.compile(r'(?P<attr>\b(?:href|src|action|poster|data-src)=)(?P<quote>["\'])/(?!(?:/|webterm/web/))(?P<path>[^"\']*)', re.I)
        text = attr_pattern.sub(lambda m: f'{m.group("attr")}{m.group("quote")}{proxy_base}{m.group("path")}', text)

        # Named targets (WebFig menu: target="graphs", "winbox", "license") open
        # new browser windows outside the iframe; force in-frame navigation.
        # _blank/_self/_parent/_top are left untouched.
        if 'text/html' in ctype:
            target_pattern = re.compile(r'\btarget\s*=\s*(["\'])(?!_)[^"\']*\1', re.I)
            text = target_pattern.sub('target="_self"', text)

        css_pattern = re.compile(r'url\((?P<quote>["\']?)/(?!(?:/|webterm/web/))(?P<path>[^)"\']+)(?P=quote)\)', re.I)
        text = css_pattern.sub(lambda m: f'url({m.group("quote")}{proxy_base}{m.group("path")}{m.group("quote")})', text)

        # Device UIs often navigate from JavaScript using absolute strings such as
        # "/html/login.html". Rewrite only known web paths so regex literals like
        # /^"/ keep their closing slash untouched.
        if 'text/html' in ctype or 'javascript' in ctype or 'application/json' in ctype:
            root_path = self.JS_ROOT_PATH
            root_string = re.compile(r'(?P<quote>["\'])/(?!(?:/|webterm/web/))(?P<path>' + root_path + r')(?P=quote)', re.I)
            text = root_string.sub(lambda m: f'{m.group("quote")}{proxy_base}{m.group("path")}{m.group("quote")}', text)

            escaped_proxy_base = proxy_base.replace('/', r'\/')
            escaped_root_path = self.JS_ROOT_PATH.replace('/', r'\/')
            escaped_root_string = re.compile(r'(?P<quote>["\'])\\/(?!(?:\\/|webterm\\/web\\/))(?P<path>' + escaped_root_path + r')(?P=quote)', re.I)
            text = escaped_root_string.sub(lambda m: f'{m.group("quote")}{escaped_proxy_base}{m.group("path")}{m.group("quote")}', text)

            # Named link targets assigned from JS (WebFig: graphs.lastChild
            # .target='graphs') open new browser windows; keep them in-frame.
            js_target = re.compile(r'\.target\s*=\s*(["\'])(?!_)[^"\']*\1', re.I)
            text = js_target.sub(".target='_self'", text)

            # Device UIs navigate to the site root on logout/error (WebFig:
            # location.replace('/' + hash)). Left alone, the iframe loads the
            # Zabbix origin root inside the WebTerm window; point it at the
            # device landing page (proxy base) instead.
            root_nav = re.compile(
                r'\b(?:window\.)?location\.(?:replace|assign)\((?P<quote>["\'])/(?P=quote)',
                re.I,
            )
            text = root_nav.sub(lambda m: m.group(0)[:-2] + proxy_base + m.group('quote'), text)
            root_href = re.compile(
                r'\b(?:window\.)?location\.href\s*=\s*(?P<quote>["\'])/(?P=quote)',
                re.I,
            )
            text = root_href.sub(lambda m: m.group(0)[:-2] + proxy_base + m.group('quote'), text)

        return text

    @staticmethod
    def _strip_phantom_angular_tags(text: str) -> str:
        """Remove missing Angular module scripts and root styles.css stub from SPA HTML."""
        # Matches src="runtime.js", src="./main.js", src="/webui/polyfills.js", etc.
        text = re.sub(
            r'<script\b(?=[^>]*\btype\s*=\s*["\']module["\'])'
            r'(?=[^>]*\bsrc\s*=\s*["\'](?:[^"\']*/)?(?:runtime|polyfills|main)\.js(?:\?[^"\']*)?["\'])'
            r'[^>]*>\s*</script\s*>',
            '',
            text,
            flags=re.I,
        )
        text = re.sub(
            r'<script\b(?=[^>]*\btype\s*=\s*["\']module["\'])'
            r'(?=[^>]*\bsrc\s*=\s*["\'](?:[^"\']*/)?(?:runtime|polyfills|main)\.js(?:\?[^"\']*)?["\'])'
            r'[^>]*/>',
            '',
            text,
            flags=re.I,
        )

        def _drop_stub_styles_link(match: re.Match) -> str:
            tag = match.group(0)
            href_m = re.search(r'\bhref\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if not href_m:
                return tag
            href = href_m.group(1)
            if re.search(r'(?:^|/)styles\.css(?:\?|#|$)', href, re.I) and 'assets/styles/' not in href.lower():
                return ''
            return tag

        text = re.sub(r'<link\b[^>]*>', _drop_stub_styles_link, text, flags=re.I)
        return text

    @staticmethod
    def _history_shim(proxy_base: str) -> str:
        base = json.dumps(proxy_base)
        return (
            '<script>(function(){var b=' + base + ';window.__WEBTERM_PROXY_BASE__=b;'
            'function n(u){try{if(!u||typeof u!=="string")return u;'
            'if(u.indexOf(b)===0)return u;'
            'if(/^https?:\\/\\//i.test(u)){var x=new URL(u,window.location.href);'
            'if(x.origin!==window.location.origin)return u;u=x.pathname+x.search+x.hash}'
            'if(u.charAt(0)==="/"){if(u.indexOf("/webterm/web/")===0)return u;'
            'return b+u.replace(/^\\/+/g,"")}return u}catch(e){return u}}'
            '["pushState","replaceState"].forEach(function(k){var f=history[k];'
            'if(!f||f.__webtermWrapped)return;var w=function(s,t,u){return f.call(this,s,t,n(u))};'
            'w.__webtermWrapped=true;history[k]=w});'
            'if(window.fetch&&!window.fetch.__webtermWrapped){var of=window.fetch;'
            'var wf=function(i,init){try{if(typeof i==="string")i=n(i);'
            'else if(i&&i.url)i=new Request(n(i.url),i)}catch(e){}return of.call(this,i,init)};'
            'wf.__webtermWrapped=true;window.fetch=wf}'
            'if(window.XMLHttpRequest){var xo=XMLHttpRequest.prototype.open;'
            'if(xo&&!xo.__webtermWrapped){var wxo=function(m,u){try{u=n(u)}catch(e){}'
            'return xo.apply(this,[m,u].concat([].slice.call(arguments,2)))};'
            'wxo.__webtermWrapped=true;XMLHttpRequest.prototype.open=wxo}}'
            'if(window.open&&!window.open.__webtermWrapped){var oo=window.open;'
            'var wo=function(u,t,f){try{u=n(u)}catch(e){}return oo.call(this,u,t,f)};'
            'wo.__webtermWrapped=true;window.open=wo}'
            '})();</script>'
        )

    def _log_suspicious_swarm_response(self, target_url: str, status: int, content_type: str, payload: bytes):
        if 'swarm.cgi' not in target_url:
            return
        if status < 400 and len(payload) >= 512:
            return
        sample = payload[:240].decode('utf-8', errors='replace')
        sample = re.sub(r'\s+', ' ', sample).strip()
        self.logger.info(
            'Web swarm.cgi short/error response status=%s bytes=%s content_type=%s url=%s sample=%s',
            status, len(payload), content_type or '-', target_url, sample
        )
