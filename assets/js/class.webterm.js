/**
 * WebTerm by Elitech Solutions
 * Single floating terminal with tabs (SecureCRT style)
 */
(function() {
    'use strict';

    function buildWebSocketUrl() {
        var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var path = window.ZABBIX_WEBTERM_WS_PATH || '/webterm/ws';
        return protocol + '//' + window.location.host + path;
    }

    const WS_URL = buildWebSocketUrl();
    const DEVELOPER_URL = 'https://www.linkedin.com/in/srodriguezxs/';
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

    function modeBadge(mode) {
        if (mode === 'telnet') return 'TEL';
        if (mode === 'web') return 'WEB';
        return 'SSH';
    }

    function poweredByHtml() {
        return '<span class="wt-powered-label">Powered by</span>' +
            '<span class="wt-powered-brand">Elitech Solutions</span>' +
            '<span class="wt-powered-sep">·</span>' +
            '<a class="wt-powered-dev" href="' + DEVELOPER_URL + '" target="_blank" rel="noopener noreferrer" title="Simon Alex Rodriguez Saavedra">S. Rodríguez</a>';
    }

    function buildWebProxyBase(sessionToken) {
        var path = window.ZABBIX_WEBTERM_WEB_PATH || '/webterm/web';
        return path.replace(/\/$/, '') + '/' + sessionToken + '/';
    }
    const sessions = [];
    let activeSession = null;
    let win = null;
    let sessionCounter = 0;
    let minimized = false;
    let maximized = false;
    let prevRect = null;
    const STORAGE_KEY = 'webterm-state';
    let saveStateTimeout = null;
    const MIN_WINDOW_WIDTH = 500;
    const MIN_WINDOW_HEIGHT = 300;
    const VIEWPORT_MARGIN = 8;

    function getViewportSize() {
        return {
            width: Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0),
            height: Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0)
        };
    }

    function getInitialWindowHeight() {
        var viewport = getViewportSize();
        return Math.max(MIN_WINDOW_HEIGHT, Math.floor(viewport.height * 0.5));
    }

    function clampWindowToViewport() {
        if (!win || maximized || minimized) return;

        var viewport = getViewportSize();
        var maxWidth = Math.max(320, viewport.width - (VIEWPORT_MARGIN * 2));
        var maxHeight = Math.max(240, viewport.height - (VIEWPORT_MARGIN * 2));
        var width = Math.min(Math.max(win.offsetWidth || 780, Math.min(MIN_WINDOW_WIDTH, maxWidth)), maxWidth);
        var height = Math.min(Math.max(win.offsetHeight || getInitialWindowHeight(), Math.min(MIN_WINDOW_HEIGHT, maxHeight)), maxHeight);
        var left = win.offsetLeft;
        var top = win.offsetTop;

        left = Math.min(Math.max(VIEWPORT_MARGIN, left), Math.max(VIEWPORT_MARGIN, viewport.width - width - VIEWPORT_MARGIN));
        top = Math.min(Math.max(VIEWPORT_MARGIN, top), Math.max(VIEWPORT_MARGIN, viewport.height - height - VIEWPORT_MARGIN));

        win.style.width = width + 'px';
        win.style.height = height + 'px';
        win.style.left = left + 'px';
        win.style.top = top + 'px';
    }

    function buildWebUrl(host, port, protocol) {
        var cleanHost = (host || '').trim();
        if (!cleanHost) return '';
        if (/^https?:\/\//i.test(cleanHost)) return cleanHost;

        var proto = protocol || (String(port) === '443' ? 'https' : 'http');
        var portText = port ? String(port) : '';
        var defaultPort = (proto === 'https') ? '443' : '80';
        var needsPort = portText && portText !== defaultPort;

        if (cleanHost.indexOf(':') >= 0 && cleanHost.charAt(0) !== '[' && cleanHost.split(':').length > 2) {
            cleanHost = '[' + cleanHost + ']';
        }

        return proto + '://' + cleanHost + (needsPort ? ':' + portText : '/');
    }

    // === Persistence functions ===
    function saveState() {
        try {
            // Don't save if no sessions (clean state)
            if (sessions.length === 0) {
                localStorage.removeItem(STORAGE_KEY);
                return;
            }

            const state = {
                sessions: sessions.map(function(s) {
                    return {
                        id: s.id,
                        label: s.label,
                        hostName: s.hostName,
                        mode: s.mode,
                        host: s.host,
                        port: s.port,
                        webProtocol: s.webProtocol || null,
                        sessionToken: s.sessionToken || null,
                        webProxyUrl: s.webProxyUrl || (s.sessionToken ? buildWebProxyBase(s.sessionToken) : null),
                        targetUrl: s.targetUrl || null,
                        url: s.url || null,
                        connected: false
                    };
                }),
                window: {
                    left: win ? win.offsetLeft : 80,
                    top: win ? win.offsetTop : 60,
                    width: win ? win.offsetWidth : 800,
                    height: win ? win.offsetHeight : getInitialWindowHeight(),
                    minimized: minimized,
                    maximized: maximized
                },
                activeSessionId: activeSession ? activeSession.id : null,
                savedAt: Date.now()
            };

            localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        } catch (e) {
            console.warn('[WebTerm] Failed to save state:', e);
        }
    }

    function saveStateThrottled() {
        // Throttle saves during drag/resize (500ms)
        if (saveStateTimeout) clearTimeout(saveStateTimeout);
        saveStateTimeout = setTimeout(saveState, 500);
    }

    // === sessionStorage for session tokens only (NO credentials) ===
    const SESSION_STORAGE_KEY = 'webterm-session-tokens';

    function saveSessionToken(sessionId, sessionToken) {
        try {
            var data = localStorage.getItem(SESSION_STORAGE_KEY);
            var tokens = data ? JSON.parse(data) : {};
            tokens[sessionId] = {
                sessionToken: sessionToken,
                savedAt: Date.now()
            };
            localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(tokens));
        } catch (e) {
            console.warn('[WebTerm] Failed to save session token:', e);
        }
    }

    function getSessionToken(sessionId) {
        try {
            var data = localStorage.getItem(SESSION_STORAGE_KEY);
            if (!data) return null;
            var tokens = JSON.parse(data);
            var token = tokens[sessionId];
            // Check if token is expired (8 hours - matches proxy timeout)
            if (token && Date.now() - token.savedAt > 8 * 60 * 60 * 1000) {
                removeSessionToken(sessionId);
                return null;
            }
            return token ? token.sessionToken : null;
        } catch (e) {
            return null;
        }
    }

    function removeSessionToken(sessionId) {
        try {
            var data = localStorage.getItem(SESSION_STORAGE_KEY);
            if (!data) return;
            var tokens = JSON.parse(data);
            delete tokens[sessionId];
            if (Object.keys(tokens).length === 0) {
                localStorage.removeItem(SESSION_STORAGE_KEY);
            } else {
                localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(tokens));
            }
        } catch (e) {
            console.warn('[WebTerm] Failed to remove session token:', e);
        }
    }

    function clearAllSessionTokens() {
        try {
            localStorage.removeItem(SESSION_STORAGE_KEY);
        } catch (e) {}
    }

    function isPageRefresh() {
        // Check if this is a page refresh (F5) vs new navigation
        // Using performance.navigation for older browsers or performance.getEntriesByType for newer
        if (window.performance && performance.navigation) {
            return performance.navigation.type === 1; // TYPE_RELOAD
        }
        // Fallback: check if we have a state to restore (indicates refresh in our context)
        return localStorage.getItem(STORAGE_KEY) !== null;
    }

    function restoreState() {
        try {
            var data = localStorage.getItem(STORAGE_KEY);
            if (!data) return false;

            var state = JSON.parse(data);
            if (!state || !state.sessions || state.sessions.length === 0) {
                localStorage.removeItem(STORAGE_KEY);
                return false;
            }

            // Check if state is too old (7 days)
            if (state.savedAt && Date.now() - state.savedAt > 7 * 24 * 60 * 60 * 1000) {
                localStorage.removeItem(STORAGE_KEY);
                return false;
            }

            // Restore window state
            minimized = state.window && state.window.minimized ? true : false;
            maximized = state.window && state.window.maximized ? true : false;

            // Create window if not minimized
            if (!minimized) {
                ensureWindow();
                if (state.window && win) {
                    if (!maximized) {
                        win.style.left = (state.window.left || 80) + 'px';
                        win.style.top = (state.window.top || 60) + 'px';
                        win.style.width = (state.window.width || 800) + 'px';
                        win.style.height = (state.window.height || getInitialWindowHeight()) + 'px';
                        clampWindowToViewport();
                    } else {
                        // Maximized state
                        var viewport = getViewportSize();
                        win.style.left = '0px';
                        win.style.top = '0px';
                        win.style.width = viewport.width + 'px';
                        win.style.height = viewport.height + 'px';
                    }
                }
            } else {
                // Minimized - show taskbar button
                ensureWindow();
                if (win) win.style.display = 'none';
            }

            // Find max session counter from restored sessions
            var maxCounter = 0;

            // Restore session metadata (not actual connections)
            state.sessions.forEach(function(s) {
                var id = s.id;
                var label = s.label;
                var hostName = s.hostName;
                var mode = s.mode;
                var host = s.host;
                var port = s.port;
                var webProtocol = s.webProtocol || (String(port) === '443' ? 'https' : 'http');
                var sessionToken = s.sessionToken || null;
                var webProxyUrl = s.webProxyUrl || (sessionToken ? buildWebProxyBase(sessionToken) : null);
                var targetUrl = s.targetUrl || s.url || (mode === 'web' ? buildWebUrl(host, port, webProtocol) : null);
                var url = s.url || targetUrl;

                // Extract counter from id (wt-s-N)
                var match = id.match(/wt-s-(\d+)/);
                if (match) {
                    var num = parseInt(match[1], 10);
                    if (num > maxCounter) maxCounter = num;
                }

                // Create placeholder session
                var termDiv = document.createElement('div');
                termDiv.id = id + '-term';
                termDiv.className = 'wt-term-pane';
                termDiv.style.display = 'none';
                document.getElementById('wt-body').appendChild(termDiv);

                var session = {
                    id: id,
                    label: label,
                    hostName: hostName,
                    mode: mode,
                    host: host,
                    port: port,
                    webProtocol: webProtocol,
                    sessionToken: sessionToken,
                    webProxyUrl: webProxyUrl,
                    targetUrl: targetUrl,
                    url: url,
                    termDiv: termDiv,
                    terminal: null,
                    fitAddon: null,
                    ws: null,
                    connected: false
                };
                sessions.push(session);
                if (mode === 'web') {
                    renderWebSession(session);
                    setTimeout(function() {
                        if (!session._isConnecting && !session.connected) {
                            initWebSession(session);
                        }
                    }, 100);
                }
            });

            sessionCounter = maxCounter;

            // Restore active session
            if (state.activeSessionId) {
                var active = sessions.find(function(s) { return s.id === state.activeSessionId; });
                if (active) activeSession = active;
            }

            if (sessions.length > 0 && !activeSession) {
                activeSession = sessions[0];
            }

            // Check for auto-reconnect with stored tokens only (no credentials)
            var hasAnyTokens = false;
            sessions.forEach(function(s) {
                var token = getSessionToken(s.id);
                if (token) {
                    hasAnyTokens = true;
                    // Mark for reconnection attempt
                    s._autoReconnect = true;
                }
            });

            // Show disconnected message only for sessions without stored tokens
            sessions.forEach(function(s) {
                if (s.mode === 'web') {
                    return;
                }
                if (!s._autoReconnect) {
                    // Create a simple disconnected notice
                    var notice = document.createElement('div');
                    notice.className = 'wt-reconnect-notice';
                    notice.innerHTML =
                        '<div style="padding:20px;text-align:center;color:#e45959;">' +
                        '<p>Sesión desconectada</p>' +
                        '<button class="btn-alt" style="margin-top:10px;">Reconectar</button>' +
                        '</div>';
                    notice.querySelector('button').addEventListener('click', function() {
                        showLoginDialog(s.hostName, s.host, s.mode);
                        // Pre-fill the dialog with saved values
                        setTimeout(function() {
                            var hostInput = document.getElementById('wt-host');
                            var modeInput = document.getElementById('wt-mode');
                            var portInput = document.getElementById('wt-port');
                            if (hostInput) hostInput.value = s.host;
                            if (modeInput) modeInput.value = s.mode;
                            if (portInput) portInput.value = s.port;
                        }, 50);
                    });
                    s.termDiv.appendChild(notice);
                }
                s.termDiv.style.display = 'none';
            });

            renderTabs();
            if (activeSession) {
                switchTab(activeSession);
            }
            updateStatus(activeSession || sessions[0]);

            // Auto-reconnect sessions with stored tokens (using reattachment)
            // Process reconnections with delay between each to avoid race conditions
            if (hasAnyTokens && !window._webtermIsRestoring) {
                window._webtermIsRestoring = true;
                loadXterm(function() {
                    var reconnectQueue = sessions.filter(function(s) { return s._autoReconnect; });
                    var reconnectedCount = 0;

                    reconnectQueue.forEach(function(s, index) {
                        setTimeout(function() {
                            if (s._isConnecting || s.connected) {
                                reconnectedCount++;
                                if (reconnectedCount >= reconnectQueue.length) {
                                    window._webtermIsRestoring = false;
                                }
                                return; // Skip if already connecting or connected
                            }

                            s._isConnecting = true;
                            var token = getSessionToken(s.id);

                            if (token) {
                                // Reattach directly using token — no credentials needed!
                                reattachSession(s, token);
                            }
                            delete s._autoReconnect;

                            reconnectedCount++;
                            if (reconnectedCount >= reconnectQueue.length) {
                                setTimeout(function() {
                                    window._webtermIsRestoring = false;
                                }, 2000);
                            }
                        }, index * 1000); // 1 second delay between each session
                    });

                    // Safety: clear the flag after max time
                    setTimeout(function() {
                        window._webtermIsRestoring = false;
                    }, reconnectQueue.length * 1000 + 5000);
                });
            }

            // Update taskbar if minimized
            if (minimized) {
                var taskbar = document.getElementById('wt-taskbar');
                if (taskbar) {
                    taskbar.innerHTML = '';
                    var btn = document.createElement('button');
                    btn.className = 'wt-taskbar-btn';
                    btn.innerHTML = '<span class="wt-brand-mark"></span> WebTerm <span class="wt-taskbar-count">' + sessions.length + '</span>';
                    btn.addEventListener('click', restoreWindow);
                    taskbar.appendChild(btn);
                    taskbar.style.display = 'flex';
                }
            }

            return true;
        } catch (e) {
            console.warn('[WebTerm] Failed to restore state:', e);
            return false;
        }
    }

    // Save state on navigation without interrupting normal Zabbix workflows.
    window.addEventListener('beforeunload', function(e) {
        if (sessions.length > 0) {
            saveState();
        }
    });

    // Helper: extract IP from a table row or nearby DOM elements
    function extractHostInfoFromDOM(trigger_element) {
        var info = { name: '', ip: '' };
        try {
            // trigger_element might be jQuery or DOM element
            var el = trigger_element;
            if (el && el.jquery) el = el[0]; // unwrap jQuery
            if (!el) return info;

            // Find the closest table row
            var row = el.closest ? el.closest('tr') : null;
            if (!row) return info;

            var cells = row.querySelectorAll('td');
            // Host name from first cell
            if (cells[0]) info.name = cells[0].textContent.trim();

            // Search all cells for IP pattern (e.g. "127.0.0.1:10050" or "192.168.1.1")
            for (var i = 0; i < cells.length; i++) {
                var txt = cells[i].textContent.trim();
                var m = txt.match(/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::\d+)?/);
                if (m) { info.ip = m[1]; break; }
            }
        } catch (e) {}
        return info;
    }

    // Monkey-patch host popup menu
    if (typeof getMenuPopupHost === 'function') {
        const _orig = getMenuPopupHost;
        getMenuPopupHost = function(options, trigger_element) {
            const sections = _orig(options, trigger_element);
            if (options.hostid) {
                // Extract info NOW (while trigger_element is still in DOM)
                var domInfo = extractHostInfoFromDOM(trigger_element);
                var _hid = options.hostid;
                var _hname = options.host_name || domInfo.name || 'Host';
                var _hip = domInfo.ip || '';

                sections.push({
                    label: t('Connect'),
                    items: [
                        { label: 'SSH', clickCallback: function() { openConnectForm(_hid, 'ssh', null, _hname, _hip); } },
                        { label: 'Telnet', clickCallback: function() { openConnectForm(_hid, 'telnet', null, _hname, _hip); } },
                        { label: 'Web', clickCallback: function() { openConnectForm(_hid, 'web', null, _hname, _hip); } }
                    ]
                });
            }
            return sections;
        };
    }

    function openConnectForm(hostid, mode, trigger_element, hostNameFromOptions, preExtractedIp) {
        var hostName = hostNameFromOptions || 'Host';

        // 1) Use pre-extracted IP if available
        if (preExtractedIp) {
            showLoginDialog(hostName, preExtractedIp, mode);
            return;
        }

        // 2) Fallback: fetch from Zabbix API
        fetch('zabbix.php?action=webterm.hostinfo&hostid=' + hostid, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        })
        .then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(function(data) {
            var ip = data.ip || '';
            var name = (data.host ? data.host.name : '') || hostName;
            showLoginDialog(name, ip, mode);
        })
        .catch(function() {
            // Last resort: open dialog without IP
            showLoginDialog(hostName, '', mode);
        });
    }

    function showLoginDialog(hostName, ip, mode) {
        var defPort = mode === 'web' ? '80' : (mode === 'telnet' ? '23' : '22');
        var content = jQuery('<div class="wt-login">').append(
            jQuery('<div class="wt-login-head">').append(
                jQuery('<span class="wt-login-host">').text(hostName),
                jQuery('<span class="wt-login-mode">').text(modeBadge(mode))
            ),
            jQuery('<div class="wt-login-grid">').append(
                jQuery('<label for="wt-host">IP / DNS</label>'),
                jQuery('<input id="wt-host" type="text">').val(ip),
                jQuery('<label for="wt-mode">Modo</label>'),
                jQuery('<select id="wt-mode"><option value="ssh">SSH</option><option value="telnet">Telnet</option><option value="web">Web</option></select>').val(mode),
                jQuery('<label class="wt-web-field" for="wt-web-protocol">Protocolo</label>'),
                jQuery('<select class="wt-web-field" id="wt-web-protocol"><option value="http">HTTP</option><option value="https">HTTPS</option></select>'),
                jQuery('<label for="wt-port">Puerto</label>'),
                jQuery('<input id="wt-port" type="number">').val(defPort),
                jQuery('<label class="wt-auth-field" for="wt-user">Usuario</label>'),
                jQuery('<input class="wt-auth-field" id="wt-user" type="text" placeholder="root" autocomplete="username">'),
                jQuery('<label class="wt-auth-field" for="wt-pass">Contraseña</label>'),
                jQuery('<input class="wt-auth-field" id="wt-pass" type="password" autocomplete="current-password">')
            ),
            jQuery('<div class="wt-login-foot">').append(
                jQuery('<span>').text('Powered by Elitech Solutions · '),
                jQuery('<a>').attr({
                    href: DEVELOPER_URL,
                    target: '_blank',
                    rel: 'noopener noreferrer'
                }).text('Simon Alex Rodriguez Saavedra')
            )
        );

        function syncModeFields() {
            var selected = content.find('#wt-mode').val();
            var portEl = content.find('#wt-port');
            content.find('.wt-web-field').toggle(selected === 'web');
            content.find('.wt-auth-field').toggle(selected !== 'web');
            content.find('.wt-login-mode').text(modeBadge(selected));

            if (selected === 'web') {
                if (portEl.val() === '22' || portEl.val() === '23' || !portEl.val()) portEl.val('80');
            } else {
                portEl.val(selected === 'ssh' ? '22' : '23');
            }
        }

        content.find('#wt-mode').on('change', syncModeFields);
        content.find('#wt-web-protocol').on('change', function() {
            var portEl = content.find('#wt-port');
            if (portEl.val() === '80' || portEl.val() === '443' || !portEl.val()) {
                portEl.val(this.value === 'https' ? '443' : '80');
            }
        });
        syncModeFields();

        overlayDialogue({
            title: 'Conectar — ' + hostName,
            class: 'modal-popup',
            content: content,
            buttons: [{
                title: 'Conectar', isSubmit: true,
                action: function() {
                    var h = document.getElementById('wt-host').value.trim();
                    var m = document.getElementById('wt-mode').value;
                    var protocol = document.getElementById('wt-web-protocol').value;
                    var p = parseInt(document.getElementById('wt-port').value) || (m === 'web' ? (protocol === 'https' ? 443 : 80) : (m === 'ssh' ? 22 : 23));
                    var u = document.getElementById('wt-user').value.trim();
                    var pw = document.getElementById('wt-pass').value;
                    if (!h) { postMessageError('Ingrese IP/DNS'); return false; }
                    if (m === 'web') {
                        addWebSession(hostName, h, p, protocol);
                        return true;
                    }
                    if (m === 'ssh' && !u) { postMessageError('Ingrese usuario'); return false; }
                    addSession(hostName, m, h, p, u, pw);
                    return true;
                }
            }, { title: 'Cancelar', class: 'btn-alt', action: function(){} }]
        }, jQuery(document.body));
    }

    // === Main floating window ===
    function ensureWindow() {
        // Check if window exists in our reference
        if (win) {
            // Check if it still exists in DOM (Zabbix might have removed it)
            if (!document.getElementById('webterm-main')) {
                // Zabbix removed it, we need to recreate but keep session state
                console.log('[WebTerm] Window was removed from DOM, recreating...');
                win = null; // Force recreation
            } else {
                if (minimized) restoreWindow();
                win.style.display = '';
                bringToFront(win);
                return;
            }
        }

        // Check if there's a window in DOM from previous page state
        var existingWin = document.getElementById('webterm-main');
        if (existingWin) {
            existingWin.remove(); // Remove stale window
        }

        win = document.createElement('div');
        win.id = 'webterm-main';
        win.className = 'wt-window';
        win.innerHTML =
            '<div class="wt-titlebar">' +
                '<div class="wt-title-block">' +
                    '<span class="wt-brand-mark"></span>' +
                    '<div class="wt-title-text">' +
                        '<span class="wt-title">WebTerm</span>' +
                        '<span class="wt-title-sub">Elitech Solutions</span>' +
                    '</div>' +
                '</div>' +
                '<div class="wt-title-btns">' +
                    '<button class="wt-tbtn wt-btn-min" title="Minimizar">\u2500</button>' +
                    '<button class="wt-tbtn wt-btn-max" title="Maximizar">\u2610</button>' +
                    '<button class="wt-tbtn wt-btn-close" title="Cerrar todo">\u2715</button>' +
                '</div>' +
            '</div>' +
            '<div class="wt-tabs" id="wt-tabs"></div>' +
            '<div class="wt-body" id="wt-body"></div>' +
            '<div class="wt-footer">' +
                '<div class="wt-status" id="wt-status">' +
                    '<span class="wt-status-dot" id="wt-status-dot">\u25CF</span>' +
                    '<span id="wt-status-text">Listo</span>' +
                '</div>' +
                '<div class="wt-powered">' + poweredByHtml() + '</div>' +
            '</div>' +
            '<div class="wt-resize-handle"></div>';

        win.style.left = '80px';
        win.style.top = '60px';
        win.style.height = getInitialWindowHeight() + 'px';
        win.style.zIndex = '99999';
        // Move to end of body to ensure it's on top of everything
        document.body.appendChild(win);
        // Ensure it's visible
        win.style.display = '';

        makeDraggable(win, win.querySelector('.wt-titlebar'));
        makeResizable(win, win.querySelector('.wt-resize-handle'));

        win.querySelector('.wt-btn-min').addEventListener('click', minimizeWindow);
        win.querySelector('.wt-btn-max').addEventListener('click', toggleMaximize);
        win.querySelector('.wt-btn-close').addEventListener('click', closeAll);
        win.addEventListener('mousedown', function() { bringToFront(win); });
        clampWindowToViewport();
        bringToFront(win);
    }

    function minimizeWindow() {
        minimized = true;
        win.style.display = 'none';

        var taskbar = document.getElementById('wt-taskbar');
        if (!taskbar) {
            taskbar = document.createElement('div');
            taskbar.id = 'wt-taskbar';
            taskbar.className = 'wt-taskbar';
            document.body.appendChild(taskbar);
        }
        taskbar.innerHTML = '';
        var btn = document.createElement('button');
        btn.className = 'wt-taskbar-btn';
        btn.innerHTML = '<span class="wt-brand-mark"></span> WebTerm <span class="wt-taskbar-count">' + sessions.length + '</span>';
        btn.addEventListener('click', restoreWindow);
        taskbar.appendChild(btn);
        taskbar.style.display = 'flex';
        saveState();
    }

    function restoreWindow() {
        minimized = false;
        if (win) win.style.display = '';
        bringToFront(win);
        var taskbar = document.getElementById('wt-taskbar');
        if (taskbar) taskbar.style.display = 'none';
        saveState();
        if (activeSession && activeSession.fitAddon) {
            setTimeout(function() { activeSession.fitAddon.fit(); }, 50);
        }
    }

    function toggleMaximize() {
        if (maximized) {
            if (prevRect) {
                win.style.left = prevRect.left + 'px';
                win.style.top = prevRect.top + 'px';
                win.style.width = prevRect.width + 'px';
                win.style.height = prevRect.height + 'px';
            }
            maximized = false;
            clampWindowToViewport();
        } else {
            var viewport = getViewportSize();
            prevRect = { left: win.offsetLeft, top: win.offsetTop, width: win.offsetWidth, height: win.offsetHeight };
            win.style.left = '0px';
            win.style.top = '0px';
            win.style.width = viewport.width + 'px';
            win.style.height = viewport.height + 'px';
            maximized = true;
        }
        saveState();
        if (activeSession && activeSession.fitAddon) {
            setTimeout(function() { activeSession.fitAddon.fit(); }, 100);
        }
    }

    function closeAll() {
        sessions.forEach(function(s) {
            if (s.ws) try { s.ws.close(); } catch(e) {}
            if (s.terminal) try { s.terminal.dispose(); } catch(e) {}
        });
        sessions.length = 0;
        activeSession = null;
        if (win) { win.remove(); win = null; }
        var taskbar = document.getElementById('wt-taskbar');
        if (taskbar) taskbar.remove();
        clearAllSessionTokens();
        saveState();
    }

    // === Session management ===
    function addWebSession(hostName, host, port, protocol) {
        ensureWindow();
        sessionCounter++;
        var id = 'wt-s-' + sessionCounter;
        var targetUrl = buildWebUrl(host, port, protocol);
        var label = hostName + ' — WEB ' + targetUrl;

        var termDiv = document.createElement('div');
        termDiv.id = id + '-term';
        termDiv.className = 'wt-term-pane wt-web-pane';
        termDiv.style.display = 'none';
        document.getElementById('wt-body').appendChild(termDiv);

        var session = {
            id: id, label: label, hostName: hostName, mode: 'web', host: host, port: port,
            webProtocol: protocol, targetUrl: targetUrl, url: targetUrl,
            sessionToken: null, webProxyUrl: null,
            termDiv: termDiv, terminal: null, fitAddon: null, ws: null, connected: false
        };

        sessions.push(session);
        renderWebSession(session);
        saveState();
        renderTabs();
        switchTab(session);
        initWebSession(session);
    }

    function initWebSession(session) {
        var ws = new WebSocket(WS_URL);
        session.ws = ws;
        session._isConnecting = true;
        renderTabs();
        updateStatus(session);

        ws.onopen = function() {
            if (session.sessionToken) {
                ws.send(JSON.stringify({ reattach: true, session_token: session.sessionToken }));
                return;
            }
            ws.send(JSON.stringify({
                mode: 'web',
                host: session.host,
                port: session.port,
                protocol: session.webProtocol || (String(session.port) === '443' ? 'https' : 'http')
            }));
        };

        ws.onmessage = function(event) {
            var data;
            try { data = JSON.parse(event.data); } catch (e) { return; }
            if (data.connected && data.mode === 'web') {
                session.connected = true;
                session._isConnecting = false;
                session.sessionToken = data.session_token;
                session.webProxyUrl = data.web_url || buildWebProxyBase(data.session_token);
                session.targetUrl = data.target_url || session.targetUrl;
                session.url = session.targetUrl;
                session.label = session.hostName + ' — WEB ' + session.targetUrl;
                renderWebSession(session);
                saveState();
                renderTabs();
                updateStatus(session);
                try { ws.close(); } catch (e) {}
                session.ws = null;
                return;
            }
            if (data.error && data.reattach_failed && session.sessionToken) {
                // Stored session is gone server-side: drop the token and open a
                // fresh web session over the same socket.
                session.sessionToken = null;
                session.webProxyUrl = null;
                ws.send(JSON.stringify({
                    mode: 'web',
                    host: session.host,
                    port: session.port,
                    protocol: session.webProtocol || (String(session.port) === '443' ? 'https' : 'http')
                }));
                return;
            }
            if (data.error) {
                session.connected = false;
                session._isConnecting = false;
                showWebError(session, data.error);
                saveState();
                renderTabs();
                updateStatus(session);
            }
        };

        ws.onerror = function() {
            session.connected = false;
            session._isConnecting = false;
            showWebError(session, 'No se pudo crear la sesión web en el proxy');
            renderTabs();
            updateStatus(session);
        };
    }

    function recreateWebSession(session) {
        if (!session || session.mode !== 'web') return;
        if (session.ws) {
            try { session.ws.close(); } catch (e) {}
        }
        session.ws = null;
        session.sessionToken = null;
        session.webProxyUrl = null;
        session.connected = false;
        session._isConnecting = false;
        renderWebSession(session);
        saveState();
        initWebSession(session);
    }

    function isAcceptableWebOrigin(origin) {
        // In subdomain mode the web iframe is a different origin
        // (https://<subid>.<base>), so accept any active web session's origin
        // in addition to the Zabbix origin itself.
        if (!origin) return true;
        if (origin === window.location.origin) return true;
        return sessions.some(function(s) {
            if (s.mode !== 'web' || !s.webProxyUrl) return false;
            try { return new URL(s.webProxyUrl, window.location.href).origin === origin; }
            catch (e) { return false; }
        });
    }

    window.addEventListener('message', function(event) {
        if (!isAcceptableWebOrigin(event.origin)) return;
        var data = event.data || {};
        if (!data || data.type !== 'webterm-web-session-expired') return;
        var token = data.token || null;
        var session = sessions.find(function(s) {
            return s.mode === 'web' && (!token || s.sessionToken === token);
        });
        if (session && !session._isConnecting) {
            recreateWebSession(session);
        }
    });

    function proxyUrlForWebTarget(session, target) {
        var proxyBase = session.webProxyUrl || (session.sessionToken ? buildWebProxyBase(session.sessionToken) : '');
        if (!proxyBase) return '';
        if (!target) return proxyBase;
        if (target.indexOf(proxyBase) === 0) return target;

        var value = String(target).trim();
        var path = value;
        if (/^https?:\/\//i.test(value)) {
            try {
                var url = new URL(value);
                path = url.pathname.replace(/^\//, '') + url.search + url.hash;
            } catch (e) {
                path = '';
            }
        } else {
            path = value.replace(/^\//, '');
        }
        return proxyBase + path;
    }

    function showWebError(session, message) {
        if (!session || !session.termDiv) return;
        session.termDiv.textContent = '';
        var box = document.createElement('div');
        box.className = 'wt-web-error';
        var title = document.createElement('strong');
        title.textContent = 'No se pudo abrir el modo Web';
        var detail = document.createElement('span');
        detail.textContent = String(message || 'Error desconocido');
        box.appendChild(title);
        box.appendChild(detail);
        session.termDiv.appendChild(box);
    }

    function renderWebSession(session) {
        if (!session || !session.termDiv) return;
        session.termDiv.innerHTML = '';
        session.termDiv.classList.add('wt-web-pane');

        if (!session.connected || !session.webProxyUrl) {
            session.termDiv.innerHTML = '<div class="wt-web-loading">Creando sesión web desde Zabbix...</div>';
            return;
        }

        var shell = document.createElement('div');
        shell.className = 'wt-web-shell';

        var toolbar = document.createElement('div');
        toolbar.className = 'wt-web-toolbar';

        var urlInput = document.createElement('input');
        urlInput.className = 'wt-web-url';
        urlInput.type = 'text';
        urlInput.value = session.targetUrl || session.url || buildWebUrl(session.host, session.port, session.webProtocol);
        urlInput.title = 'URL del equipo; el tráfico sale desde Zabbix';

        var reloadBtn = document.createElement('button');
        reloadBtn.className = 'wt-web-btn';
        reloadBtn.type = 'button';
        reloadBtn.title = 'Recargar';
        reloadBtn.textContent = '↻';

        var openBtn = document.createElement('button');
        openBtn.className = 'wt-web-btn';
        openBtn.type = 'button';
        openBtn.title = 'Abrir en nueva pestaña';
        openBtn.textContent = '↗';

        var iframe = document.createElement('iframe');
        iframe.className = 'wt-web-iframe';
        iframe.referrerPolicy = 'no-referrer';
        var recoveringEscapedWebPath = false;

        function isKnownWebRootPath(pathname) {
            return /^\/(?:html\/|jscripts\/|styles\/|images\/|i18n\/|help\/|fonts\/|screens\/|skins\/|skin\/|assets\/|common\/|lib\/|features\/|cgi-bin\/|monitoring\/|api\/|webui\/|restconf\/|webfig(?:\/|$)|jsproxy(?:\/|$)|graphs(?:\/|$)|files\/|localizationdir\b|modern_ui_installed\b|helpdir\b|swarm\.cgi\b|styles\.css\b|login\b|logincheck\b|logout\b|favicon\.ico\b)/i.test(pathname || '');
        }

        function recoverEscapedWebPath() {
            if (recoveringEscapedWebPath || !session.webProxyUrl) return;
            // Subdomain mode: the iframe is cross-origin and root-relative URLs
            // already resolve to the proxy, so there is nothing to recover.
            if (/^https?:\/\//i.test(session.webProxyUrl)) return;
            try {
                var loc = iframe.contentWindow.location;
                var publicPath = (window.ZABBIX_WEBTERM_WEB_PATH || '/webterm/web').replace(/\/$/, '');
                if (loc.origin !== window.location.origin) return;
                if (loc.pathname.indexOf(publicPath + '/') === 0) return;
                if (!isKnownWebRootPath(loc.pathname)) return;
                recoveringEscapedWebPath = true;
                var escapedTarget = loc.pathname + loc.search + loc.hash;
                iframe.src = proxyUrlForWebTarget(session, escapedTarget);
                setTimeout(function() { recoveringEscapedWebPath = false; }, 500);
            } catch (e) {}
        }

        iframe.addEventListener('load', recoverEscapedWebPath);

        function navigate(target) {
            var value = (target || '').trim();
            if (!value) value = session.targetUrl || session.url || buildWebUrl(session.host, session.port, session.webProtocol);
            if (!/^https?:\/\//i.test(value) && value.charAt(0) !== '/') {
                value = buildWebUrl(session.host, session.port, session.webProtocol).replace(/\/$/, '') + '/' + value;
            }
            session.targetUrl = value;
            session.url = value;
            urlInput.value = value;
            iframe.src = proxyUrlForWebTarget(session, value);
            session.label = session.hostName + ' — WEB ' + value;
            saveState();
            renderTabs();
            updateStatus(session);
        }

        reloadBtn.addEventListener('click', function() {
            try { iframe.contentWindow.location.reload(); } catch (e) { iframe.src = proxyUrlForWebTarget(session, session.targetUrl); }
        });
        openBtn.addEventListener('click', function() {
            window.open(proxyUrlForWebTarget(session, session.targetUrl || urlInput.value), '_blank', 'noopener');
        });
        urlInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') navigate(urlInput.value);
        });

        toolbar.appendChild(urlInput);
        toolbar.appendChild(reloadBtn);
        toolbar.appendChild(openBtn);
        shell.appendChild(toolbar);
        shell.appendChild(iframe);
        session.termDiv.appendChild(shell);
        navigate(urlInput.value);
    }

    function addSession(hostName, mode, host, port, username, password) {
        ensureWindow();
        sessionCounter++;
        var id = 'wt-s-' + sessionCounter;
        var label = hostName + ' \u2014 ' + mode.toUpperCase() + ' ' + host + ':' + port;

        var termDiv = document.createElement('div');
        termDiv.id = id + '-term';
        termDiv.className = 'wt-term-pane';
        termDiv.style.display = 'none';
        document.getElementById('wt-body').appendChild(termDiv);

        var session = {
            id: id, label: label, hostName: hostName, mode: mode, host: host, port: port,
            termDiv: termDiv, terminal: null, fitAddon: null, ws: null, connected: false,
            _pendingCreds: { host: host, port: port, username: username, password: password }
        };
        sessions.push(session);
        saveState();
        renderTabs();
        switchTab(session);

        loadXterm(function() { initSession(session, host, port, mode, username, password); });
    }

    function initSession(session, host, port, mode, username, password, reattachToken) {
        // Prevent duplicate connections
        if (session.ws && (session.ws.readyState === WebSocket.CONNECTING || session.ws.readyState === WebSocket.OPEN)) {
            console.log('[WebTerm] Already connected or connecting, skipping initSession');
            return;
        }
        session._isConnecting = true;

        var terminal = new Terminal({
            cursorBlink: true, fontSize: 13,
            fontFamily: '"Fira Code","Cascadia Code","JetBrains Mono","Courier New",monospace',
            theme: XTERM_THEME,
            scrollback: 10000, allowProposedApi: true
        });

        var fitAddon = new FitAddon.FitAddon();
        terminal.loadAddon(fitAddon);
        if (typeof WebLinksAddon !== 'undefined') {
            terminal.loadAddon(new WebLinksAddon.WebLinksAddon());
        }

        terminal.open(session.termDiv);
        fitAddon.fit();
        session.terminal = terminal;
        session.fitAddon = fitAddon;
        session.sessionToken = null;  // Will be set on successful connection

        new ResizeObserver(function() {
            if (session === activeSession) fitAddon.fit();
        }).observe(session.termDiv);

        if (reattachToken) {
            terminal.writeln('\x1b[33m  Reanudando sesión en ' + host + ':' + port + '...\x1b[0m\r\n');
        } else if (session._pendingCreds && session._pendingCreds._isReconnect) {
            terminal.writeln('\x1b[33m  Reconectando a ' + host + ':' + port + '...\x1b[0m\r\n');
        } else {
            terminal.writeln('\x1b[33m  Conectando a ' + host + ':' + port + '...\x1b[0m\r\n');
        }

        // WebSocket — use text mode to avoid Blob issues
        var ws = new WebSocket(WS_URL);
        ws.binaryType = 'arraybuffer';
        session.ws = ws;

        ws.onopen = function() {
            // If we have a reattach token, try to reattach first
            if (reattachToken) {
                ws.send(JSON.stringify({reattach: true, session_token: reattachToken}));
            } else {
                ws.send(JSON.stringify({mode: mode, host: host, port: port, username: username, password: password}));
            }
        };

        ws.onmessage = function(event) {
            var str;
            if (event.data instanceof ArrayBuffer) {
                str = new TextDecoder('utf-8').decode(new Uint8Array(event.data));
            } else {
                str = String(event.data);
            }

            // Only try JSON parse if it looks like a JSON control message
            // Control messages are short and start with {
            if (str.length < 500 && str.charAt(0) === '{') {
                try {
                    var j = JSON.parse(str);

                    // Handle successful connection (new or reattached)
                    if (j.connected) {
                        session.connected = true;
                        session._isConnecting = false;  // Clear connecting flag

                        // Store session token for future reattachment
                        if (j.session_token) {
                            session.sessionToken = j.session_token;
                        }

                        if (j.reattached) {
                            // Successfully reattached to existing session
                            terminal.writeln('\x1b[32m✓ Sesión reanudada\x1b[0m\r\n');
                        } else {
                            // New connection established
                            terminal.writeln('\x1b[32m\u2713 Conectado\x1b[0m\r\n');
                        }

                        ws.send(JSON.stringify({resize: true, cols: terminal.cols, rows: terminal.rows}));
                        terminal.focus();

                        // Save session token for auto-reconnect on page refresh (NO credentials)
                        if (session.sessionToken) {
                            saveSessionToken(session.id, session.sessionToken);
                        }
                        if (session._pendingCreds) {
                            delete session._pendingCreds;
                        }
                        saveState();
                        renderTabs();
                        updateStatus(session);
                        return;
                    }

                    // Handle reattach failure - fall back to new connection
                    if (j.reattach_failed) {
                        terminal.writeln('\x1b[33m! Sesión expirada, reconectando...\x1b[0m\r\n');
                        // Try new connection with stored credentials
                        if (session._pendingCreds) {
                            ws.send(JSON.stringify({
                                mode: mode,
                                host: session._pendingCreds.host,
                                port: session._pendingCreds.port,
                                username: session._pendingCreds.username,
                                password: session._pendingCreds.password
                            }));
                            // Note: we're continuing the connection attempt, so don't trigger reconnectComplete yet
                        } else {
                            session._isConnecting = false;  // Clear connecting flag
                            terminal.writeln('\r\n\x1b[31m\u2717 ' + (j.error || 'Reattach failed') + '\x1b[0m');
                            session.connected = false;
                            saveState();
                            renderTabs();
                            updateStatus(session);
                        }
                        return;
                    }

                    if (j.disconnected) {
                        session.connected = false;
                        session.sessionToken = null;
                        saveState();
                        terminal.writeln('\r\n\x1b[31m\u2717 Sesi\u00f3n cerrada\x1b[0m');
                        renderTabs();
                        updateStatus(session);
                        return;
                    }
                    if (j.error) {
                        session.connected = false;
                        session._isConnecting = false;  // Clear connecting flag
                        saveState();
                        terminal.writeln('\r\n\x1b[31m\u2717 ' + j.error + '\x1b[0m');
                        renderTabs();
                        updateStatus(session);
                        return;
                    }
                } catch(e) {
                    // Not valid JSON, just terminal data that starts with {
                }
            }

            // Terminal data — write to xterm
            terminal.write(str);
        };

        ws.onerror = function(event) {
            console.error('[WebTerm] WebSocket error', event);
            session.connected = false;
            session._isConnecting = false;  // Clear connecting flag
            saveState();
            terminal.writeln('\r\n\x1b[31m\u2717 Error de conexi\u00f3n WebSocket\x1b[0m');
            updateStatus(session);
        };

        ws.onclose = function(event) {
            console.log('[WebTerm] WebSocket closed, code=' + event.code + ' reason=' + event.reason);
            session.connected = false;
            session._isConnecting = false;  // Clear connecting flag
            saveState();
            renderTabs();
            updateStatus(session);
        };

        terminal.onData(function(d) {
            if (ws.readyState === WebSocket.OPEN) {
                try { ws.send(d); } catch(e) { console.error('[WebTerm] send error', e); }
            }
        });

        terminal.onResize(function(size) {
            if (ws.readyState === WebSocket.OPEN) {
                try { ws.send(JSON.stringify({resize: true, cols: size.cols, rows: size.rows})); } catch(e) {}
            }
        });
    }

    // Reattach to an existing proxy session using a token (no credentials needed)
    function reattachSession(session, token) {
        // Clean up any existing terminal or notices
        session.termDiv.innerHTML = '';

        var terminal = new Terminal({
            cursorBlink: true, fontSize: 13,
            fontFamily: '"Fira Code","Cascadia Code","JetBrains Mono","Courier New",monospace',
            theme: XTERM_THEME,
            scrollback: 10000, allowProposedApi: true
        });

        var fitAddon = new FitAddon.FitAddon();
        terminal.loadAddon(fitAddon);
        if (typeof WebLinksAddon !== 'undefined') {
            terminal.loadAddon(new WebLinksAddon.WebLinksAddon());
        }

        terminal.open(session.termDiv);
        fitAddon.fit();
        session.terminal = terminal;
        session.fitAddon = fitAddon;

        new ResizeObserver(function() {
            if (session === activeSession) fitAddon.fit();
        }).observe(session.termDiv);

        terminal.writeln('\x1b[33m  Reanudando sesión en ' + session.host + ':' + session.port + '...\x1b[0m\r\n');

        var ws = new WebSocket(WS_URL);
        ws.binaryType = 'arraybuffer';
        session.ws = ws;

        ws.onopen = function() {
            ws.send(JSON.stringify({ reattach: true, session_token: token }));
        };

        ws.onmessage = function(event) {
            var str;
            if (event.data instanceof ArrayBuffer) {
                str = new TextDecoder('utf-8').decode(new Uint8Array(event.data));
            } else {
                str = String(event.data);
            }

            if (str.length < 500 && str.charAt(0) === '{') {
                try {
                    var j = JSON.parse(str);

                    if (j.connected && j.reattached) {
                        session.connected = true;
                        session._isConnecting = false;
                        session.sessionToken = j.session_token;
                        terminal.writeln('\x1b[32m✓ Sesión reanudada\x1b[0m\r\n');
                        ws.send(JSON.stringify({ resize: true, cols: terminal.cols, rows: terminal.rows }));
                        terminal.focus();
                        saveSessionToken(session.id, session.sessionToken);
                        saveState();
                        renderTabs();
                        updateStatus(session);
                        return;
                    }

                    if (j.reattach_failed) {
                        session._isConnecting = false;
                        terminal.writeln('\x1b[31m✗ Sesión expirada\x1b[0m');
                        removeSessionToken(session.id);
                        session.connected = false;
                        renderTabs();
                        updateStatus(session);
                        return;
                    }

                    if (j.disconnected) {
                        session.connected = false;
                        session.sessionToken = null;
                        removeSessionToken(session.id);
                        terminal.writeln('\r\n\x1b[31m✗ Sesión cerrada\x1b[0m');
                        saveState();
                        renderTabs();
                        updateStatus(session);
                        return;
                    }

                    if (j.error) {
                        session.connected = false;
                        session._isConnecting = false;
                        terminal.writeln('\r\n\x1b[31m✗ ' + j.error + '\x1b[0m');
                        renderTabs();
                        updateStatus(session);
                        return;
                    }
                } catch (e) {}
            }

            // Terminal data
            terminal.write(str);
        };

        ws.onerror = function() {
            session.connected = false;
            session._isConnecting = false;
            terminal.writeln('\r\n\x1b[31m✗ Error de conexión WebSocket\x1b[0m');
            updateStatus(session);
        };

        ws.onclose = function() {
            session.connected = false;
            session._isConnecting = false;
            renderTabs();
            updateStatus(session);
        };

        terminal.onData(function(d) {
            if (ws.readyState === WebSocket.OPEN) {
                try { ws.send(d); } catch (e) {}
            }
        });

        terminal.onResize(function(size) {
            if (ws.readyState === WebSocket.OPEN) {
                try { ws.send(JSON.stringify({ resize: true, cols: size.cols, rows: size.rows })); } catch (e) {}
            }
        });
    }

    function removeSession(session) {
        if (session.ws) try { session.ws.close(); } catch(e) {}
        if (session.terminal) try { session.terminal.dispose(); } catch(e) {}
        session.termDiv.remove();
        removeSessionToken(session.id);

        var idx = sessions.indexOf(session);
        if (idx >= 0) sessions.splice(idx, 1);
        saveState();

        if (sessions.length === 0) { closeAll(); return; }

        if (activeSession === session) {
            switchTab(sessions[Math.min(idx, sessions.length - 1)]);
        }
        renderTabs();
    }

    function switchTab(session) {
        sessions.forEach(function(s) { s.termDiv.style.display = 'none'; });
        session.termDiv.style.display = '';
        activeSession = session;
        saveState();
        if (session.fitAddon) setTimeout(function() { session.fitAddon.fit(); }, 30);
        if (session.terminal) session.terminal.focus();
        renderTabs();
        updateStatus(session);
    }

    function updateStatus(session) {
        if (session !== activeSession) return;
        var dot = document.getElementById('wt-status-dot');
        var txt = document.getElementById('wt-status-text');
        if (!dot || !txt) {
            // Elements may have been removed by Zabbix update
            return;
        }
        if (session.connected) {
            dot.style.color = '#59e45b';
            if (session.mode === 'web') {
                txt.textContent = 'Web desde Zabbix — ' + (session.targetUrl || session.url || buildWebUrl(session.host, session.port, session.webProtocol));
            } else {
                txt.textContent = 'Conectado — ' + session.mode.toUpperCase() + ' ' + session.host + ':' + session.port;
            }
        } else {
            dot.style.color = '#e45959';
            txt.textContent = 'Desconectado';
        }
    }

    function renderTabs() {
        var tabsEl = document.getElementById('wt-tabs');
        if (!tabsEl) {
            // Zabbix may have removed our window, trigger recreation
            if (sessions.length > 0) {
                console.log('[WebTerm] Tabs container missing, triggering recreation...');
                setTimeout(recreateWindowAfterRemoval, 100);
            }
            return;
        }
        tabsEl.innerHTML = '';

        sessions.forEach(function(s) {
            var tab = document.createElement('div');
            tab.className = 'wt-tab' + (s === activeSession ? ' wt-tab-active' : '');

            var dot = document.createElement('span');
            dot.className = 'wt-tab-dot';
            dot.style.color = s.connected ? '#59e45b' : (s.ws && s.ws.readyState === WebSocket.CONNECTING ? '#ffc859' : '#e45959');
            dot.textContent = '\u25CF';

            var lbl = document.createElement('span');
            lbl.className = 'wt-tab-label';
            lbl.textContent = s.hostName;
            lbl.title = s.label;

            var close = document.createElement('span');
            close.className = 'wt-tab-close';
            close.textContent = '\u2715';
            close.title = 'Cerrar sesi\u00f3n';
            close.addEventListener('click', (function(sess) {
                return function(e) { e.stopPropagation(); removeSession(sess); };
            })(s));

            tab.appendChild(dot);
            tab.appendChild(lbl);
            var badge = document.createElement('span');
            badge.className = 'wt-tab-badge';
            badge.textContent = modeBadge(s.mode);
            tab.appendChild(badge);
            tab.appendChild(close);
            tab.addEventListener('click', (function(sess) {
                return function() { switchTab(sess); };
            })(s));
            tabsEl.appendChild(tab);
        });

        var titleEl = win ? win.querySelector('.wt-title') : null;
        if (titleEl && activeSession) {
            titleEl.textContent = activeSession.label;
        }

        var taskbarCount = document.querySelector('.wt-taskbar-count');
        if (taskbarCount) taskbarCount.textContent = sessions.length;
    }

    // === Draggable ===
    function makeDraggable(el, handle) {
        var dragging = false, sx, sy, ox, oy;
        handle.addEventListener('mousedown', function(e) {
            if (e.target.closest('.wt-tbtn')) return;
            dragging = true; sx = e.clientX; sy = e.clientY;
            ox = el.offsetLeft; oy = el.offsetTop;
            document.addEventListener('mousemove', mv);
            document.addEventListener('mouseup', up);
            e.preventDefault();
        });
        function mv(e) {
            if (!dragging) return;
            var viewport = getViewportSize();
            var width = el.offsetWidth;
            var height = el.offsetHeight;
            var left = ox + e.clientX - sx;
            var top = oy + e.clientY - sy;
            left = Math.min(Math.max(VIEWPORT_MARGIN, left), Math.max(VIEWPORT_MARGIN, viewport.width - width - VIEWPORT_MARGIN));
            top = Math.min(Math.max(VIEWPORT_MARGIN, top), Math.max(VIEWPORT_MARGIN, viewport.height - height - VIEWPORT_MARGIN));
            el.style.left = left + 'px';
            el.style.top = top + 'px';
        }
        function up() { dragging = false; document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); clampWindowToViewport(); saveStateThrottled(); }
    }

    // === Resizable ===
    function makeResizable(el, handle) {
        var resizing = false, sx, sy, sw, sh;
        handle.addEventListener('mousedown', function(e) {
            resizing = true; sx = e.clientX; sy = e.clientY; sw = el.offsetWidth; sh = el.offsetHeight;
            document.addEventListener('mousemove', mv);
            document.addEventListener('mouseup', up);
            e.preventDefault();
        });
        function mv(e) {
            if (!resizing) return;
            var viewport = getViewportSize();
            var maxWidth = Math.max(320, viewport.width - el.offsetLeft - VIEWPORT_MARGIN);
            var maxHeight = Math.max(240, viewport.height - el.offsetTop - VIEWPORT_MARGIN);
            var minWidth = Math.min(MIN_WINDOW_WIDTH, maxWidth);
            var minHeight = Math.min(MIN_WINDOW_HEIGHT, maxHeight);
            el.style.width = Math.min(Math.max(minWidth, sw + e.clientX - sx), maxWidth) + 'px';
            el.style.height = Math.min(Math.max(minHeight, sh + e.clientY - sy), maxHeight) + 'px';
        }
        function up() { resizing = false; document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up);
            clampWindowToViewport(); if (activeSession && activeSession.fitAddon) activeSession.fitAddon.fit(); saveStateThrottled(); }
    }

    var topZ = 20000;
    function bringToFront(w) { topZ++; w.style.zIndex = topZ; }

    window.addEventListener('resize', function() {
        if (!win) return;
        if (maximized) {
            var viewport = getViewportSize();
            win.style.width = viewport.width + 'px';
            win.style.height = viewport.height + 'px';
        } else {
            clampWindowToViewport();
        }
        if (activeSession && activeSession.fitAddon) {
            setTimeout(function() { activeSession.fitAddon.fit(); }, 50);
        }
        saveStateThrottled();
    });

    // === Restore state on page load ===
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            restoreState();
            startPersistenceWatchdog();
        });
    } else {
        // DOM already loaded
        restoreState();
        startPersistenceWatchdog();
    }

    // === Persistence Watchdog - Prevents Zabbix from destroying the modal ===
    function startPersistenceWatchdog() {
        // Watch for DOM changes that might remove our window
        var observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(mutation) {
                if (mutation.type === 'childList') {
                    // Check if our window was removed
                    if (win && win.parentNode === null) {
                        console.log('[WebTerm] Window removed from DOM, restoring...');
                        // Recreate the window and restore sessions
                        recreateWindowAfterRemoval();
                    }
                }
            });
        });

        // Start observing
        observer.observe(document.body, { childList: true, subtree: true });

        // Also use a periodic check as backup (but not during initial restore)
        setInterval(function() {
            if (sessions.length > 0 && (!win || win.parentNode === null) && !window._webtermIsRestoring) {
                console.log('[WebTerm] Periodic check: Window missing, restoring...');
                recreateWindowAfterRemoval();
            }
        }, 2000);
    }

    function recreateWindowAfterRemoval() {
        // Prevent multiple simultaneous recreations
        if (recreateWindowAfterRemoval._isRunning) {
            return;
        }
        recreateWindowAfterRemoval._isRunning = true;

        // Save current state
        saveState();

        // Clear window reference so ensureWindow creates a new one
        win = null;

        // Recreate window
        ensureWindow();

        // Restore tabs UI
        renderTabs();

        // Re-attach terminal divs
        sessions.forEach(function(s) {
            if (s.termDiv && s.termDiv.parentNode !== document.getElementById('wt-body')) {
                var body = document.getElementById('wt-body');
                if (body) {
                    body.appendChild(s.termDiv);
                    s.termDiv.style.display = (s === activeSession) ? '' : 'none';
                }
            }
        });

        // Switch to active session
        if (activeSession) {
            switchTab(activeSession);
        }

        // Trigger reconnection for disconnected sessions (only if WebSocket is closed)
        sessions.forEach(function(s) {
            if (s.mode === 'web') {
                return;
            }
            if (!s.connected && !s._isConnecting) {
                // Check if we need to reconnect
                if (!s.ws || s.ws.readyState === WebSocket.CLOSED) {
                    var token = getSessionToken(s.id);
                    if (token) {
                        // Show reconnect notice instead of auto-connecting
                        var notice = document.createElement('div');
                        notice.className = 'wt-reconnect-notice';
                        notice.innerHTML =
                            '<div style="padding:20px;text-align:center;color:#ffc859;">' +
                            '<p>Sesión disponible para reanudar</p>' +
                            '<button class="btn-alt" style="margin-top:10px;">Reanudar sesión</button>' +
                            '</div>';
                        notice.querySelector('button').addEventListener('click', function() {
                            showLoginDialog(s.hostName, s.host, s.mode);
                            setTimeout(function() {
                                var hostInput = document.getElementById('wt-host');
                                var modeInput = document.getElementById('wt-mode');
                                var portInput = document.getElementById('wt-port');
                                if (hostInput) hostInput.value = s.host;
                                if (modeInput) modeInput.value = s.mode;
                                if (portInput) portInput.value = s.port;
                            }, 50);
                        });
                        // Clear any existing notice
                        var existingNotice = s.termDiv.querySelector('.wt-reconnect-notice');
                        if (existingNotice) existingNotice.remove();
                        s.termDiv.appendChild(notice);
                    }
                }
            }
        });

        // Clear flag after a delay
        setTimeout(function() {
            recreateWindowAfterRemoval._isRunning = false;
        }, 500);
    }

    // === Dynamic xterm loader ===
    function loadXterm(cb) {
        if (typeof Terminal !== 'undefined' && typeof FitAddon !== 'undefined') { cb(); return; }
        var base = 'https://cdn.jsdelivr.net/npm/';
        var res = [
            { t: 'css', u: base + 'xterm@5.3.0/css/xterm.min.css' },
            { t: 'js', u: base + 'xterm@5.3.0/lib/xterm.min.js' },
            { t: 'js', u: base + 'xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js' },
            { t: 'js', u: base + 'xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.min.js' }
        ];
        var n = 0;
        function done() { if (++n >= res.length) setTimeout(cb, 100); }
        res.forEach(function(r) {
            if (r.t === 'css') {
                var l = document.createElement('link'); l.rel = 'stylesheet'; l.href = r.u;
                l.onload = done; l.onerror = done; document.head.appendChild(l);
            } else {
                var s = document.createElement('script'); s.src = r.u;
                s.onload = done; s.onerror = done; document.head.appendChild(s);
            }
        });
    }
})();
