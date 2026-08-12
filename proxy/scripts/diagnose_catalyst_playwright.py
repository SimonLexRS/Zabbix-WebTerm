#!/usr/bin/env python3
"""Diagnose Catalyst webui through Zabbix WebTerm using Playwright.

Flow:
  1. Open the Zabbix dashboard URL
  2. Pause for interactive login (Zabbix + optional Catalyst web login)
  3. Capture screenshots, console errors, failed network, iframe URL
  4. Write a JSON + markdown report under diag-output/

Pause modes:
  --pause-mode signal (default on servers without TTY/DISPLAY):
      wait until CONTINUE_FILE exists (touch the file after login)
  --pause-mode input:
      wait for Enter in the terminal after login
  --pause-mode inspector:
      Playwright page.pause() (requires headed + inspector)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

DEFAULT_URL = (
    'http://ia020203:8081/zabbix.php?action=dashboard.view'
    '&dashboardid=396&from=now-24h&to=now'
)
CONTINUE_FILE = Path('/tmp/webterm-pw-continue')
OUTPUT_DIR = Path(__file__).resolve().parent / 'diag-output'


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def wait_for_login(mode: str, timeout_sec: int) -> None:
    CONTINUE_FILE.unlink(missing_ok=True)
    print()
    print('=' * 72)
    print('PAUSA PARA LOGIN')
    print('1) En la ventana del navegador (o en tu Chrome si usas signal),')
    print('   inicia sesión en Zabbix.')
    print('2) Abre el modo Web del Catalyst (Connect > Web) hasta ver el')
    print('   estado que quieres diagnosticar (login WLC o UI rota).')
    if mode == 'signal':
        print(f'3) Cuando esté listo, ejecuta:  touch {CONTINUE_FILE}')
        print('   (o crea ese archivo de otro modo).')
    elif mode == 'input':
        print('3) Vuelve a esta terminal y pulsa Enter.')
    else:
        print('3) En Playwright Inspector, pulsa Resume cuando hayas terminado.')
    print('=' * 72)
    print(flush=True)

    deadline = time.time() + timeout_sec
    if mode == 'inspector':
        return
    if mode == 'input':
        try:
            input('Pulsa Enter cuando el login/UI esté listo... ')
        except EOFError:
            print('Sin TTY; cambiando a modo signal.', flush=True)
            mode = 'signal'
        else:
            return

    print(f'Esperando archivo {CONTINUE_FILE} (timeout {timeout_sec}s)...', flush=True)
    while time.time() < deadline:
        if CONTINUE_FILE.exists():
            print('Señal recibida; continuando captura.', flush=True)
            CONTINUE_FILE.unlink(missing_ok=True)
            return
        time.sleep(1)
    raise TimeoutError(f'Tiempo agotado esperando {CONTINUE_FILE}')


def collect_frames(page) -> list[dict[str, Any]]:
    frames = []
    for frame in page.frames:
        info: dict[str, Any] = {
            'name': frame.name,
            'url': frame.url,
            'is_webterm': '/webterm/web/' in (frame.url or ''),
        }
        try:
            info['title'] = frame.title()
        except Exception as exc:
            info['title_error'] = str(exc)
        try:
            info['body_text_sample'] = (frame.locator('body').inner_text(timeout=2000) or '')[:500]
        except Exception as exc:
            info['body_text_error'] = str(exc)
        frames.append(info)
    return frames


def summarize_failures(failed: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    mime_html = []
    for item in failed:
        key = str(item.get('status') or item.get('failure') or 'unknown')
        by_status[key] = by_status.get(key, 0) + 1
        ct = (item.get('content_type') or '').lower()
        url = item.get('url') or ''
        if 'text/html' in ct and any(url.lower().endswith(ext) for ext in (
            '.css', '.js', '.mjs', '.woff', '.woff2', '.ttf', '.otf', '.map'
        )):
            mime_html.append(url)
    return {'by_status': by_status, 'html_as_static_urls': mime_html[:20]}


def write_report(out_dir: Path, report: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report['stamp']
    json_path = out_dir / f'report-{stamp}.json'
    md_path = out_dir / f'report-{stamp}.md'
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')

    lines = [
        f'# Diagnóstico Catalyst WebTerm ({stamp})',
        '',
        f'- URL inicial: `{report["start_url"]}`',
        f'- URL final página: `{report["final_url"]}`',
        f'- Título: {report.get("title")}',
        f'- Screenshot: `{report.get("screenshot")}`',
        f'- Screenshot webterm: `{report.get("screenshot_webterm")}`',
        '',
        '## Frames',
        '',
    ]
    for fr in report.get('frames', []):
        lines.append(f'- `{fr.get("url")}` title={fr.get("title")!r} webterm={fr.get("is_webterm")}')
        sample = fr.get('body_text_sample')
        if sample:
            lines.append(f'  - body: {sample[:200]!r}')
    lines += ['', '## Consola (errores)', '']
    for msg in report.get('console_errors', [])[:40]:
        lines.append(f'- [{msg.get("type")}] {msg.get("text")}')
    lines += ['', '## Red fallida (muestra)', '']
    for item in report.get('failed_requests', [])[:40]:
        lines.append(
            f'- {item.get("status") or item.get("failure")} '
            f'`{item.get("url")}` ct={item.get("content_type")!r}'
        )
    summary = report.get('failure_summary') or {}
    lines += ['', '## Resumen fallos', '', f'```json\n{json.dumps(summary, indent=2)}\n```', '']
    lines += ['## Hipótesis', '']
    for h in report.get('hypotheses', []):
        lines.append(f'- {h}')
    token = report.get('detected_token')
    if token:
        lines += ['', '## Token detectado', '', f'`{token}`', '']
    excerpt = report.get('proxy_log_excerpt') or []
    if excerpt:
        lines += ['', '## Extracto logs proxy', '', '```']
        lines.extend(excerpt[-40:])
        lines += ['```', '']
    md_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return md_path


def latest_webterm_token_from_logs(compose_dir: Path, since: str = '30m') -> str | None:
    """Best-effort: read recent proxy access logs for an active /webterm/web/<token>/ path."""
    import re
    import subprocess

    try:
        proc = subprocess.run(
            ['docker', 'compose', 'logs', f'--since={since}', 'webterm-proxy'],
            cwd=str(compose_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    text = proc.stdout or ''
    tokens = re.findall(r'/webterm/web/([A-Za-z0-9_-]{20,})/', text)
    return tokens[-1] if tokens else None


def proxy_log_excerpt(compose_dir: Path, token: str | None, since: str = '30m') -> list[str]:
    import subprocess

    try:
        proc = subprocess.run(
            ['docker', 'compose', 'logs', f'--since={since}', 'webterm-proxy'],
            cwd=str(compose_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return [f'log_error: {exc}']
    lines = (proc.stdout or '').splitlines()
    if token:
        lines = [ln for ln in lines if token in ln]
    # Prefer interesting lines
    interesting = [
        ln for ln in lines
        if any(k in ln for k in ('soft-404', 'app-bundle', 'index.html', 'ERROR', 'WARNING', 'woff', 'styles.css', 'runtime.js'))
    ]
    return (interesting or lines)[-80:]


def build_hypotheses(report: dict[str, Any]) -> list[str]:
    hyps: list[str] = []
    mime_html = (report.get('failure_summary') or {}).get('html_as_static_urls') or []
    if mime_html:
        hyps.append('Aún hay respuestas text/html para assets estáticos (regresión MIME).')
    else:
        hyps.append('No se observó HTML disfrazado de CSS/JS/fuente en la captura de red.')

    failed = report.get('failed_requests') or []
    fonts = [u for u in failed if any(x in (u.get('url') or '').lower() for x in ('.woff', '.ttf', '.otf'))]
    if fonts:
        hyps.append('Fuentes 4xx: degradación cosmética esperable si el IOS no las sirve.')
    stubs = [u for u in failed if any(x in (u.get('url') or '') for x in (
        '/styles.css', '/runtime.js', '/polyfills.js', '/main.js', 'd3-hierarchy', '/iox/'
    ))]
    if stubs:
        hyps.append('Stubs Angular/d3/iox ausentes en este IOS (404); SPA AngularJS puede seguir operativa.')

    webterm_frames = [f for f in report.get('frames', []) if f.get('is_webterm')]
    if not webterm_frames:
        hyps.append('No hay iframe /webterm/web/ abierto: hay que abrir Connect > Web tras el login.')
    else:
        body = (webterm_frames[0].get('body_text_sample') or '').lower()
        if 'login' in body and 'password' in body:
            hyps.append('El iframe sigue en login del WLC (credenciales no enviadas o sesión no autenticada).')
        elif len(body.strip()) < 20:
            hyps.append('Iframe webterm casi vacío: posible UI en blanco / bootstrap Angular fallido.')
        else:
            hyps.append('Iframe webterm tiene contenido de texto; revisar screenshot para usabilidad real.')

    console = ' '.join(m.get('text', '') for m in report.get('console_errors', [])).lower()
    if 'sfntversion' in console or "mime type ('text/html')" in console:
        hyps.append('Consola aún reporta OTS/MIME HTML: el soft-404 guard no está cubriendo algún path.')
    return hyps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('WEBTERM_DIAG_URL', DEFAULT_URL))
    parser.add_argument('--headed', action='store_true', default=bool(os.environ.get('DISPLAY')))
    parser.add_argument('--headless', action='store_true', help='Forzar headless')
    parser.add_argument(
        '--pause-mode',
        choices=('signal', 'input', 'inspector', 'none'),
        default=os.environ.get('WEBTERM_DIAG_PAUSE', 'signal'),
    )
    parser.add_argument('--timeout-login', type=int, default=int(os.environ.get('WEBTERM_DIAG_TIMEOUT', '900')))
    parser.add_argument('--out-dir', type=Path, default=OUTPUT_DIR)
    parser.add_argument('--token', default=os.environ.get('WEBTERM_DIAG_TOKEN', ''))
    parser.add_argument('--proxy-log-file', type=Path, default=None)
    args = parser.parse_args()
    if args.proxy_log_file is None and os.environ.get('WEBTERM_DIAG_PROXY_LOG'):
        args.proxy_log_file = Path(os.environ['WEBTERM_DIAG_PROXY_LOG'])

    headed = bool(args.headed) and not args.headless
    if not os.environ.get('DISPLAY'):
        headed = False
        print('Sin DISPLAY: usando Chromium headless. Haz login en tu propio navegador.', flush=True)
    stamp = utc_now()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    console_errors: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    all_responses: list[dict[str, Any]] = []

    print(f'Playwright diagnose start url={args.url} headed={headed} pause={args.pause_mode}', flush=True)

    if args.pause_mode != 'none':
        if args.pause_mode == 'inspector':
            pass  # pause after first navigation
        else:
            wait_for_login(args.pause_mode, args.timeout_login)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        context = browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 900})
        page = context.new_page()

        def on_console(msg):
            if msg.type in ('error', 'warning'):
                console_errors.append({'type': msg.type, 'text': msg.text})

        def on_page_error(exc):
            console_errors.append({'type': 'pageerror', 'text': str(exc)})

        def on_response(resp):
            try:
                req = resp.request
                url = resp.url
                ct = (resp.headers or {}).get('content-type', '')
                entry = {
                    'url': url,
                    'status': resp.status,
                    'content_type': ct,
                    'resource_type': req.resource_type,
                }
                if '/webterm/' in url or 'webui' in url:
                    all_responses.append(entry)
                if resp.status >= 400 or (
                    'text/html' in ct.lower()
                    and any(urlparse(url).path.lower().endswith(ext) for ext in (
                        '.css', '.js', '.mjs', '.woff', '.woff2', '.ttf', '.otf', '.map'
                    ))
                ):
                    failed_requests.append(entry)
            except Exception:
                pass

        def on_request_failed(req):
            failed_requests.append({
                'url': req.url,
                'failure': req.failure,
                'resource_type': req.resource_type,
            })

        page.on('console', on_console)
        page.on('pageerror', on_page_error)
        page.on('response', on_response)
        page.on('requestfailed', on_request_failed)

        page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
        screenshot_login = out_dir / f'screenshot-before-login-{stamp}.png'
        page.screenshot(path=str(screenshot_login), full_page=True)

        if args.pause_mode == 'inspector':
            print('Abriendo Playwright Inspector (Resume tras login)...', flush=True)
            page.pause()
        elif args.pause_mode != 'none':
            # Already waited before launch for signal/input; brief settle only.
            page.wait_for_timeout(1000)

        # Give SPA/iframe a moment to settle after user action
        page.wait_for_timeout(2000)
        try:
            page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass

        screenshot = out_dir / f'screenshot-page-{stamp}.png'
        page.screenshot(path=str(screenshot), full_page=True)

        frames = collect_frames(page)
        screenshot_webterm = None
        for frame in page.frames:
            if '/webterm/web/' in (frame.url or ''):
                screenshot_webterm = out_dir / f'screenshot-webterm-{stamp}.png'
                try:
                    frame.locator('body').screenshot(path=str(screenshot_webterm))
                except Exception:
                    try:
                        page.screenshot(path=str(screenshot_webterm))
                    except Exception:
                        screenshot_webterm = None
                break

        compose_dir = Path('/home/user_admin/zabbix-coolify')
        if not compose_dir.exists():
            compose_dir = Path('/work')
        token = (args.token or '').strip() or latest_webterm_token_from_logs(compose_dir)
        if args.proxy_log_file and args.proxy_log_file.exists():
            raw = args.proxy_log_file.read_text(encoding='utf-8', errors='replace').splitlines()
            if token:
                proxy_logs = [ln for ln in raw if token in ln][-80:]
            else:
                proxy_logs = raw[-80:]
        else:
            proxy_logs = proxy_log_excerpt(compose_dir, token)
        webterm_direct_url = None
        if token:
            # Always open the live proxy session the user created (headless has no Zabbix cookies).
            origin = f'{urlparse(args.url).scheme}://{urlparse(args.url).netloc}'
            webterm_direct_url = f'{origin}/webterm/web/{token}/webui/'
            print(f'Abriendo sesión webterm detectada: {webterm_direct_url}', flush=True)
            try:
                page.goto(webterm_direct_url, wait_until='domcontentloaded', timeout=60000)
                page.wait_for_timeout(8000)
                try:
                    page.wait_for_load_state('networkidle', timeout=20000)
                except Exception:
                    pass
                screenshot_webterm = out_dir / f'screenshot-webterm-{stamp}.png'
                page.screenshot(path=str(screenshot_webterm), full_page=True)
                frames = collect_frames(page)
            except Exception as exc:
                console_errors.append({'type': 'navigate_webterm', 'text': str(exc)})

        report: dict[str, Any] = {
            'stamp': stamp,
            'start_url': args.url,
            'final_url': page.url,
            'title': page.title(),
            'detected_token': token,
            'webterm_direct_url': webterm_direct_url,
            'screenshot_before_login': str(screenshot_login),
            'screenshot': str(screenshot),
            'screenshot_webterm': str(screenshot_webterm) if screenshot_webterm else None,
            'frames': frames,
            'console_errors': console_errors[-80:],
            'failed_requests': failed_requests[-120:],
            'webterm_responses_sample': all_responses[-80:],
            'proxy_log_excerpt': proxy_logs,
        }
        report['failure_summary'] = summarize_failures(report['failed_requests'])
        report['hypotheses'] = build_hypotheses(report)
        md_path = write_report(out_dir, report)

        browser.close()

    print(f'Reporte JSON/MD en {out_dir}', flush=True)
    print(f'Markdown: {md_path}', flush=True)
    print(json.dumps({
        'stamp': stamp,
        'final_url': report['final_url'],
        'webterm_frames': [f['url'] for f in frames if f.get('is_webterm')],
        'failed_count': len(report['failed_requests']),
        'console_count': len(report['console_errors']),
        'hypotheses': report['hypotheses'],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Cancelado.', file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
