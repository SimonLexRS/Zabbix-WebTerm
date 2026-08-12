<?php
/**
 * WebTerm Connect View - Elitech Solutions
 */

$host = $data['host'];
$ip = $data['ip'];
$hostid = $data['hostid'];
$mode = $data['mode'] ?? 'ssh';
$default_port = ($mode === 'web') ? 80 : (($mode === 'telnet') ? 23 : 22);

$html_page = (new CHtmlPage())
    ->setTitle(_('Terminal Web') . ($host ? ' — ' . $host['name'] : ''));

$form_list = (new CFormList())
    ->addRow(
        new CLabel(_('Equipo'), 'host_name'),
        (new CTextBox('host_name', $host ? $host['name'] : _('Desconocido'), true))
            ->setWidth(ZBX_TEXTAREA_MEDIUM_WIDTH)
    )
    ->addRow(
        new CLabel(_('Dirección IP/DNS'), 'term_host'),
        (new CTextBox('term_host', $ip))
            ->setWidth(ZBX_TEXTAREA_MEDIUM_WIDTH)
            ->setAttribute('placeholder', '192.168.1.1')
    )
    ->addRow(
        new CLabel(_('Modo'), 'term_mode'),
        (new CSelect('term_mode'))
            ->setValue($mode)
            ->addOptions(CSelect::createOptionsFromArray([
                'ssh' => 'SSH',
                'telnet' => 'Telnet',
                'web' => 'Web'
            ]))
    )
    ->addRow(
        new CLabel(_('Puerto'), 'term_port'),
        (new CNumericBox('term_port', $default_port, 5, false, false, false))
            ->setWidth(ZBX_TEXTAREA_NUMERIC_STANDARD_WIDTH)
    )
    ->addRow(
        new CLabel(_('Usuario'), 'term_user'),
        (new CTextBox('term_user', ''))
            ->setWidth(ZBX_TEXTAREA_MEDIUM_WIDTH)
            ->setAttribute('placeholder', 'root')
            ->setAttribute('autocomplete', 'username')
    )
    ->addRow(
        new CLabel(_('Contraseña'), 'term_pass'),
        (new CPassBox('term_pass', ''))
            ->setWidth(ZBX_TEXTAREA_MEDIUM_WIDTH)
            ->setAttribute('autocomplete', 'current-password')
    );

$form = (new CForm())
    ->setId('webterm-connect-form')
    ->addItem($form_list)
    ->addItem(
        (new CFormActions(
            (new CButton('btn_connect', _('Conectar')))
                ->addClass(ZBX_STYLE_BTN_ALT)
                ->setId('webterm-btn-connect')
        ))
    );

$terminal_div = (new CDiv())
    ->setId('webterm-terminal-container')
    ->addClass('wt-standalone-wrap')
    ->addStyle('display:none;');

$terminal_header = (new CDiv([
    (new CDiv([
        (new CSpan())->addClass('wt-brand-mark'),
        (new CSpan())->setId('webterm-status')
    ]))->addClass('wt-standalone-status'),
    (new CButton('btn_disconnect', _('Desconectar')))
        ->addClass(ZBX_STYLE_BTN_ALT)
        ->addClass('webterm-disconnect')
        ->setId('webterm-btn-disconnect')
]))->addClass('wt-standalone-chrome');

$terminal_box = (new CDiv())
    ->setId('webterm-terminal')
    ->addClass('wt-standalone-term');

$terminal_footer = (new CDiv([
    (new CSpan('Powered by Elitech Solutions · ')),
    (new CTag('a', true, 'Simon Alex Rodriguez Saavedra'))
        ->setAttribute('href', 'https://www.linkedin.com/in/srodriguezxs/')
        ->setAttribute('target', '_blank')
        ->setAttribute('rel', 'noopener noreferrer')
]))->addClass('wt-standalone-footer');

$terminal_div->addItem([$terminal_header, $terminal_box, $terminal_footer]);

$brand_line = (new CDiv([
    (new CSpan('Powered by Elitech Solutions · ')),
    (new CTag('a', true, 'Simon Alex Rodriguez Saavedra'))
        ->setAttribute('href', 'https://www.linkedin.com/in/srodriguezxs/')
        ->setAttribute('target', '_blank')
        ->setAttribute('rel', 'noopener noreferrer')
]))->addClass('wt-login-foot')->addStyle('border-top:0; margin-top:8px;')->setId('webterm-brand-line');

$html_page->addItem([$form, $brand_line, $terminal_div])->show();
?>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.min.js"></script>
<script>
(function() {
    function buildWebSocketUrl() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const path = window.ZABBIX_WEBTERM_WS_PATH || '/webterm/ws';
        return protocol + '//' + window.location.host + path;
    }

    const WS_URL = buildWebSocketUrl();

    function buildWebProxyBase(sessionToken) {
        const path = window.ZABBIX_WEBTERM_WEB_PATH || '/webterm/web';
        return path.replace(/\/$/, '') + '/' + sessionToken + '/';
    }
    const SESSION_KEY = 'webterm-standalone-session';
    const XTERM_THEME = {
        background: 'transparent',
        foreground: '#e5eef8',
        cursor: '#2fd6c6',
        selectionBackground: '#26364d',
        black: '#0b111a',
        red: '#e45959',
        green: '#59e45b',
        yellow: '#ffc859',
        blue: '#4FC3F7',
        magenta: '#c47aff',
        cyan: '#2fd6c6',
        white: '#e5eef8'
    };
    let terminal = null, fitAddon = null, ws = null;
    let sessionToken = null;
    let connectionMode = null, connectionHost = null, connectionPort = null;
    let connectionUser = null, connectionPass = null;

    document.getElementById('webterm-btn-connect')?.addEventListener('click', function(e) {
        e.preventDefault();
        const host = document.querySelector('[name="term_host"]').value.trim();
        const mode = document.querySelector('[name="term_mode"]').value;
        const port = parseInt(document.querySelector('[name="term_port"]').value) || (mode === 'web' ? 80 : (mode === 'ssh' ? 22 : 23));
        const user = document.querySelector('[name="term_user"]').value.trim();
        const pass = document.querySelector('[name="term_pass"]').value;
        if (!host) { postMessageError('Ingrese dirección IP/DNS'); return; }
        if (mode === 'web') { startWebBrowser(host, port); return; }
        if (mode === 'ssh' && !user) { postMessageError('Ingrese usuario'); return; }
        startTerminal(mode, host, port, user, pass);
    });

    document.getElementById('webterm-btn-disconnect')?.addEventListener('click', disconnect);

    document.querySelector('[name="term_mode"]')?.addEventListener('change', function() {
        document.querySelector('[name="term_port"]').value = this.value === 'web' ? 80 : (this.value === 'ssh' ? 22 : 23);
        const authDisplay = this.value === 'web' ? 'none' : '';
        document.querySelector('[name="term_user"]')?.closest('li')?.style && (document.querySelector('[name="term_user"]').closest('li').style.display = authDisplay);
        document.querySelector('[name="term_pass"]')?.closest('li')?.style && (document.querySelector('[name="term_pass"]').closest('li').style.display = authDisplay);
    });

    // Try to restore session on page load
    window.addEventListener('DOMContentLoaded', function() {
        try {
            const saved = sessionStorage.getItem(SESSION_KEY);
            if (saved) {
                const data = JSON.parse(saved);
                if (data.host && data.mode && data.sessionToken) {
                    // Restore form values
                    document.querySelector('[name="term_host"]').value = data.host;
                    document.querySelector('[name="term_mode"]').value = data.mode;
                    document.querySelector('[name="term_port"]').value = data.port;
                    document.querySelector('[name="term_user"]').value = data.user;
                    // Auto-reattach
                    connectionMode = data.mode;
                    connectionHost = data.host;
                    connectionPort = data.port;
                    connectionUser = data.user;
                    startTerminal(data.mode, data.host, data.port, data.user, '', data.sessionToken);
                }
            }
        } catch(e) {}
    });

    function saveSessionState() {
        try {
            if (sessionToken && connectionHost) {
                sessionStorage.setItem(SESSION_KEY, JSON.stringify({
                    mode: connectionMode,
                    host: connectionHost,
                    port: connectionPort,
                    user: connectionUser,
                    sessionToken: sessionToken
                }));
            } else {
                sessionStorage.removeItem(SESSION_KEY);
            }
        } catch(e) {}
    }

    function buildWebUrl(host, port) {
        const cleanHost = (host || '').trim();
        if (/^https?:\/\//i.test(cleanHost)) return cleanHost;
        const protocol = String(port) === '443' ? 'https' : 'http';
        const portText = port ? String(port) : '';
        const defaultPort = protocol === 'https' ? '443' : '80';
        return protocol + '://' + cleanHost + (portText && portText !== defaultPort ? ':' + portText : '/');
    }

    function setSessionUi(active) {
        const form = document.getElementById('webterm-connect-form');
        const brand = document.getElementById('webterm-brand-line');
        const term = document.getElementById('webterm-terminal-container');
        if (form) form.style.display = active ? 'none' : '';
        if (brand) brand.style.display = active ? 'none' : '';
        if (term) term.style.display = active ? 'block' : 'none';
    }

    function showProxyError(container, message) {
        if (!container) return;
        container.textContent = '';
        const el = document.createElement('div');
        el.style.cssText = 'padding:20px;text-align:center;color:#e45959;';
        el.textContent = String(message || 'Error');
        container.appendChild(el);
    }

    function startWebBrowser(host, port) {
        disconnect();
        setSessionUi(true);

        const protocol = String(port) === '443' ? 'https' : 'http';
        const targetUrl = buildWebUrl(host, port);
        const statusEl = document.getElementById('webterm-status');
        const container = document.getElementById('webterm-terminal');
        statusEl.textContent = 'Creando sesión web desde Zabbix...';
        statusEl.style.color = '#ffc859';
        container.classList.add('wt-web-pane');
        container.style.background = '#111827';
        container.innerHTML = '<div style="padding:20px;text-align:center;color:#e5eef8;">Creando sesión web desde Zabbix...</div>';

        const webWs = new WebSocket(WS_URL);
        webWs.onopen = () => {
            webWs.send(JSON.stringify({mode: 'web', host, port, protocol}));
        };
        webWs.onmessage = (event) => {
            let data;
            try { data = JSON.parse(event.data); } catch (_) { return; }
            if (data.connected && data.mode === 'web') {
                renderProxyBrowser(data.web_url || buildWebProxyBase(data.session_token), data.target_url || targetUrl);
                try { webWs.close(); } catch (_) {}
                return;
            }
            if (data.error) {
                statusEl.textContent = '● Error';
                statusEl.style.color = '#e45959';
                showProxyError(container, data.error);
            }
        };
        webWs.onerror = () => {
            statusEl.textContent = '● Error';
            statusEl.style.color = '#e45959';
            showProxyError(container, 'No se pudo crear la sesión web en el proxy');
        };
    }

    function renderProxyBrowser(proxyBase, targetUrl) {
        const statusEl = document.getElementById('webterm-status');
        const container = document.getElementById('webterm-terminal');
        statusEl.textContent = 'Web desde Zabbix — ' + targetUrl;
        statusEl.style.color = '#59e45b';
        container.innerHTML = '';

        const shell = document.createElement('div');
        shell.style.cssText = 'display:flex;flex-direction:column;width:100%;height:100%;background:#111827;';
        const bar = document.createElement('div');
        bar.style.cssText = 'display:flex;gap:6px;align-items:center;height:34px;padding:4px 8px;background:#151f2e;border-bottom:1px solid #26364d;box-sizing:border-box;';
        const input = document.createElement('input');
        input.type = 'text';
        input.value = targetUrl;
        input.style.cssText = 'flex:1;height:24px;padding:0 8px;border:1px solid #34465f;border-radius:4px;background:#0f1723;color:#e5eef8;font-size:12px;box-sizing:border-box;';
        const reload = document.createElement('button');
        reload.type = 'button';
        reload.textContent = '↻';
        reload.title = 'Recargar';
        reload.style.cssText = 'width:28px;height:24px;border:1px solid #34465f;border-radius:4px;background:#1d2a3b;color:#e5eef8;cursor:pointer;';
        const iframe = document.createElement('iframe');
        iframe.referrerPolicy = 'no-referrer';
        iframe.style.cssText = 'flex:1;width:100%;border:0;background:#fff;';
        let recoveringEscapedWebPath = false;

        function isKnownWebRootPath(pathname) {
            return /^\/(?:html\/|jscripts\/|styles\/|images\/|i18n\/|help\/|fonts\/|screens\/|skins\/|skin\/|assets\/|common\/|lib\/|features\/|cgi-bin\/|monitoring\/|api\/|webui\/|restconf\/|localizationdir\b|modern_ui_installed\b|helpdir\b|swarm\.cgi\b|styles\.css\b|login\b|logincheck\b|logout\b|favicon\.ico\b)/i.test(pathname || '');
        }

        function recoverEscapedWebPath() {
            if (recoveringEscapedWebPath) return;
            // Subdomain mode: the iframe is cross-origin and root-relative URLs
            // already resolve to the proxy, so there is nothing to recover.
            if (/^https?:\/\//i.test(proxyBase)) return;
            try {
                const loc = iframe.contentWindow.location;
                const publicPath = (window.ZABBIX_WEBTERM_WEB_PATH || '/webterm/web').replace(/\/$/, '');
                if (loc.origin !== window.location.origin) return;
                if (loc.pathname.indexOf(publicPath + '/') === 0) return;
                if (!isKnownWebRootPath(loc.pathname)) return;
                recoveringEscapedWebPath = true;
                iframe.src = proxiedUrl(loc.pathname + loc.search + loc.hash);
                setTimeout(function() { recoveringEscapedWebPath = false; }, 500);
            } catch (_) {}
        }

        iframe.addEventListener('load', recoverEscapedWebPath);

        function proxiedUrl(value) {
            if (!value) return proxyBase;
            if (value.indexOf(proxyBase) === 0) return value;
            if (/^https?:\/\//i.test(value)) {
                try {
                    const parsed = new URL(value);
                    return proxyBase + parsed.pathname.replace(/^\//, '') + parsed.search + parsed.hash;
                } catch (_) {
                    return proxyBase;
                }
            }
            return proxyBase + String(value).replace(/^\//, '');
        }

        function go(target) {
            let value = (target || '').trim() || targetUrl;
            if (!/^https?:\/\//i.test(value) && value.charAt(0) !== '/') {
                value = targetUrl.replace(/\/$/, '') + '/' + value;
            }
            input.value = value;
            iframe.src = proxiedUrl(value);
            statusEl.textContent = 'Web desde Zabbix — ' + value;
        }

        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(input.value); });
        reload.addEventListener('click', () => { try { iframe.contentWindow.location.reload(); } catch (_) { iframe.src = proxiedUrl(input.value); } });
        bar.appendChild(input);
        bar.appendChild(reload);
        shell.appendChild(bar);
        shell.appendChild(iframe);
        container.appendChild(shell);
        go(targetUrl);
    }

    function startTerminal(mode, host, port, username, password, reattachToken = null) {
        disconnect();
        setSessionUi(true);

        const statusEl = document.getElementById('webterm-status');
        statusEl.textContent = 'Conectando a ' + host + ':' + port + ' (' + mode.toUpperCase() + ')...';
        statusEl.style.color = '#ffc859';

        // Store connection params for potential reattachment
        connectionMode = mode;
        connectionHost = host;
        connectionPort = port;
        connectionUser = username;
        connectionPass = password;

        terminal = new Terminal({
            cursorBlink: true, fontSize: 14,
            fontFamily: '"Fira Code","Cascadia Code","JetBrains Mono",monospace',
            theme: XTERM_THEME,
            scrollback: 5000, allowProposedApi: true
        });

        fitAddon = new FitAddon.FitAddon();
        terminal.loadAddon(fitAddon);
        terminal.loadAddon(new WebLinksAddon.WebLinksAddon());

        const container = document.getElementById('webterm-terminal');
        container.classList.remove('wt-web-pane');
        container.innerHTML = '';
        terminal.open(container);
        fitAddon.fit();

        if (reattachToken) {
            terminal.writeln('\x1b[33m  Reanudando sesión en ' + host + '...\x1b[0m\r\n');
        } else {
            terminal.writeln('\x1b[33m  Conectando a ' + host + '...\x1b[0m\r\n');
        }

        ws = new WebSocket(WS_URL);
        ws.onopen = () => {
            if (reattachToken) {
                ws.send(JSON.stringify({reattach: true, session_token: reattachToken}));
            } else {
                ws.send(JSON.stringify({mode, host, port, username, password}));
            }
        };
        ws.onmessage = (event) => {
            try {
                const json = JSON.parse(event.data);

                if (json.connected) {
                    statusEl.textContent = '● Conectado a ' + host + ' (' + mode.toUpperCase() + ')';
                    statusEl.style.color = '#59e45b';

                    // Store session token for reattachment
                    if (json.session_token) {
                        sessionToken = json.session_token;
                        saveSessionState();
                    }

                    if (json.reattached) {
                        terminal.writeln('\x1b[32m✓ Sesión reanudada\x1b[0m\r\n');
                    } else {
                        terminal.writeln('\x1b[32m✓ Conexión establecida\x1b[0m\r\n');
                    }
                    ws.send(JSON.stringify({resize:true, cols:terminal.cols, rows:terminal.rows}));
                    return;
                }

                // Handle reattach failure - fall back to new connection
                if (json.reattach_failed) {
                    terminal.writeln('\x1b[33m! Sesión expirada, reconectando...\x1b[0m\r\n');
                    ws.send(JSON.stringify({mode, host, port, username, password}));
                    return;
                }

                if (json.disconnected) {
                    statusEl.textContent = '● Desconectado'; statusEl.style.color = '#e45959';
                    terminal.writeln('\r\n\x1b[31m✗ Conexión cerrada\x1b[0m');
                    sessionToken = null;
                    saveSessionState();
                    return;
                }
                if (json.error) {
                    statusEl.textContent = '● Error'; statusEl.style.color = '#e45959';
                    terminal.writeln('\r\n\x1b[31m✗ Error: ' + json.error + '\x1b[0m');
                    return;
                }
            } catch(_) {}
            terminal.write(event.data);
        };
        ws.onerror = () => {
            statusEl.textContent = '● Error'; statusEl.style.color = '#e45959';
            terminal.writeln('\r\n\x1b[31m✗ No se pudo conectar al proxy WebSocket\x1b[0m');
            terminal.writeln('\x1b[33mVerifique que webterm-proxy esté corriendo.\x1b[0m');
        };
        ws.onclose = () => {
            if (statusEl.style.color !== 'rgb(228, 89, 89)') {
                statusEl.textContent = '● Desconectado'; statusEl.style.color = '#e45959';
            }
        };
        terminal.onData((data) => { if (ws?.readyState === WebSocket.OPEN) ws.send(data); });
        terminal.onResize(({cols,rows}) => { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({resize:true,cols,rows})); });
        window.addEventListener('resize', () => fitAddon?.fit());
        terminal.focus();
    }

    function disconnect() {
        if (ws) { try { ws.close(); } catch(_){} ws = null; }
        if (terminal) { terminal.dispose(); terminal = null; }
        sessionToken = null;
        sessionStorage.removeItem(SESSION_KEY);
        setSessionUi(false);
    }
})();
</script>
