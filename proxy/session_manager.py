"""
Session Manager for WebTerm WebSocket Proxy
Manages persistent SSH/Telnet sessions with output buffering.
"""

import asyncio
import secrets
import time
import logging
from collections import deque
from typing import Dict, Optional, Set, Any
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)


class RingBuffer:
    """Thread-safe ring buffer for terminal output."""

    def __init__(self, max_size: int = 10000):
        self.max_size = max_size
        self._buffer = deque(maxlen=max_size)
        self._lock = asyncio.Lock()

    async def append(self, data: str):
        """Append data to buffer."""
        async with self._lock:
            self._buffer.append(data)

    async def extend(self, data: str):
        """Extend buffer with multiple lines."""
        async with self._lock:
            lines = data.split('\n')
            for line in lines:
                self._buffer.append(line + '\n' if not line.endswith('\n') else line)

    async def get_all(self) -> list:
        """Get all buffered content."""
        async with self._lock:
            return list(self._buffer)

    async def clear(self):
        """Clear the buffer."""
        async with self._lock:
            self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)


@dataclass
class PersistentSession:
    """Represents a persistent SSH/Telnet session."""
    session_id: str
    mode: str
    host: str
    port: int
    username: Optional[str] = None

    # Connection objects (set by protocol handlers)
    client: Any = None  # paramiko.SSHClient or telnetlib3.Telnet
    channel: Any = None  # paramiko.Channel or telnetlib3 writer
    telnet_reader: Any = None  # Only for telnet
    telnet_writer: Any = None  # Only for telnet

    # Session state
    buffer: RingBuffer = field(default_factory=lambda: RingBuffer(max_size=10000))
    clients: Set[Any] = field(default_factory=set)  # Connected WebSocket clients
    connected: bool = False
    connecting: bool = False
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    # SSH-specific
    ssh_shell: Any = None

    # Web reverse proxy-specific
    web_protocol: Optional[str] = None
    web_target_base: Optional[str] = None
    web_client: Any = None
    # DNS-safe per-session label used as the subdomain for Web mode
    # (https://<web_subid>.<base-domain>/). Session ids contain '_'/'-'/uppercase
    # which are invalid in DNS/SNI, so Web sessions get a dedicated label.
    web_subid: Optional[str] = None
    # Cisco webui logs in via XHR with Authorization: Basic; later asset
    # requests (fonts, CSS) often omit it — replay the value upstream.
    web_basic_auth: Optional[str] = None

    async def attach_client(self, websocket):
        """Attach a new WebSocket client to this session."""
        self.clients.add(websocket)
        self.update_activity()
        logger.info(f"Client {websocket.remote_address} attached to session {self.session_id}")

        # Send buffered content to catch up - in chunks for efficiency
        buffered = await self.buffer.get_all()
        if buffered:
            try:
                # Send in chunks of 50 lines to reduce WebSocket overhead
                chunk_size = 50
                for i in range(0, len(buffered), chunk_size):
                    chunk = ''.join(buffered[i:i+chunk_size])
                    await websocket.send(chunk)
            except Exception as e:
                logger.warning(f"Failed to send buffered content: {e}")

    async def detach_client(self, websocket):
        """Detach a WebSocket client."""
        self.clients.discard(websocket)
        self.update_activity()
        logger.info(f"Client {websocket.remote_address} detached from session {self.session_id}")

    async def broadcast(self, data: str):
        """Broadcast data to all connected clients."""
        await self.buffer.append(data)

        if not self.clients:
            return

        # Send to all connected clients
        disconnected = []
        for ws in self.clients:
            try:
                await ws.send(data)
            except Exception as e:
                logger.warning(f"Failed to send to client: {e}")
                disconnected.append(ws)

        # Clean up disconnected clients
        for ws in disconnected:
            self.clients.discard(ws)

    async def write_input(self, data: str):
        """Write input to the session channel."""
        self.last_activity = time.time()

        try:
            if self.mode == 'ssh' and self.ssh_shell:
                self.ssh_shell.send(data)
            elif self.mode == 'telnet' and self.telnet_writer:
                self.telnet_writer.write(data)
                await self.telnet_writer.drain()
        except Exception as e:
            logger.error(f"Failed to write input: {e}")
            raise

    async def resize(self, cols: int, rows: int):
        """Resize the terminal."""
        try:
            if self.mode == 'ssh' and self.ssh_shell:
                self.ssh_shell.resize_pty(width=cols, height=rows)
            elif self.mode == 'telnet':
                # Telnet doesn't have standard resize, send NAWS if supported
                pass
        except Exception as e:
            logger.warning(f"Failed to resize terminal: {e}")

    def update_activity(self):
        """Update last activity timestamp."""
        self.last_activity = time.time()

    def is_expired(self, timeout: int) -> bool:
        """Check if session has expired."""
        return time.time() - self.last_activity > timeout

    def get_info(self) -> dict:
        """Get session info for debugging."""
        return {
            'session_id': self.session_id,
            'mode': self.mode,
            'host': self.host,
            'port': self.port,
            'web_protocol': self.web_protocol,
            'web_target_base': self.web_target_base,
            'web_subid': self.web_subid,
            'web_basic_auth': bool(self.web_basic_auth),
            'connected': self.connected,
            'clients': len(self.clients),
            'buffer_size': len(self.buffer),
            'created_at': self.created_at,
            'last_activity': self.last_activity
        }


class SessionManager:
    """Manages all persistent sessions."""

    # DNS label alphabet: lowercase letters + digits. No '_' or uppercase so the
    # value is valid as a hostname label and as a TLS SNI / cookie domain.
    _DNS_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789'

    def __init__(self, config: dict):
        self.config = config
        self.sessions: Dict[str, PersistentSession] = {}
        # Index of Web-mode subdomain label -> session, for subdomain routing.
        self.web_subindex: Dict[str, PersistentSession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the session manager."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Session manager started")

    async def stop(self):
        """Stop the session manager and close all sessions."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Close all sessions
        async with self._lock:
            for session in list(self.sessions.values()):
                await self._close_session_internal(session)
            self.sessions.clear()
            self.web_subindex.clear()

        logger.info("Session manager stopped")

    def generate_session_id(self) -> str:
        """Generate a secure session token."""
        return secrets.token_urlsafe(32)

    def _generate_web_subid(self) -> str:
        """Generate a unique DNS-safe label for a Web-mode subdomain."""
        while True:
            # 22 chars of lowercase alnum -> ~113 bits of entropy, well under the
            # 63-char DNS label limit and unguessable as a capability token.
            subid = ''.join(secrets.choice(self._DNS_ALPHABET) for _ in range(22))
            if subid not in self.web_subindex:
                return subid

    async def create_session(self, mode: str, host: str, port: int,
                           username: Optional[str] = None) -> PersistentSession:
        """Create a new persistent session."""
        session_id = self.generate_session_id()

        async with self._lock:
            # Check max sessions limit
            max_sessions = self.config.get('session', {}).get('max_sessions', 100)
            if len(self.sessions) >= max_sessions:
                # Remove oldest inactive session
                oldest = min(self.sessions.values(),
                           key=lambda s: s.last_activity)
                await self._close_session_internal(oldest)
                del self.sessions[oldest.session_id]
                if oldest.web_subid:
                    self.web_subindex.pop(oldest.web_subid, None)

            session = PersistentSession(
                session_id=session_id,
                mode=mode,
                host=host,
                port=port,
                username=username
            )
            self.sessions[session_id] = session

            # Web sessions are reachable through a dedicated subdomain label.
            if mode == 'web':
                session.web_subid = self._generate_web_subid()
                self.web_subindex[session.web_subid] = session

        logger.info(f"Created session {session_id} for {mode}://{host}:{port}")
        return session

    async def get_web_session_by_subid(self, subid: str) -> Optional[PersistentSession]:
        """Get a Web-mode session by its subdomain label."""
        if not subid:
            return None
        async with self._lock:
            session = self.web_subindex.get(subid)
            if session:
                session.update_activity()
            return session

    async def get_session(self, session_id: str) -> Optional[PersistentSession]:
        """Get a session by ID."""
        async with self._lock:
            session = self.sessions.get(session_id)
            if session:
                session.update_activity()
            return session

    async def remove_session(self, session_id: str):
        """Remove a session."""
        async with self._lock:
            session = self.sessions.pop(session_id, None)
            if session:
                if session.web_subid:
                    self.web_subindex.pop(session.web_subid, None)
                await self._close_session_internal(session)
                logger.info(f"Removed session {session_id}")

    async def _close_session_internal(self, session: PersistentSession):
        """Close a session internally (without lock)."""
        session.connected = False

        # Close all client connections
        for ws in list(session.clients):
            try:
                await ws.close()
            except:
                pass
        session.clients.clear()

        # Close protocol-specific connections
        try:
            if session.mode == 'ssh':
                if session.ssh_shell:
                    session.ssh_shell.close()
                if session.client:
                    session.client.close()
            elif session.mode == 'telnet':
                if session.telnet_writer:
                    session.telnet_writer.close()
                    await session.telnet_writer.wait_closed()
            elif session.mode == 'web':
                if session.web_client:
                    await session.web_client.close()
        except Exception as e:
            logger.warning(f"Error closing session: {e}")

        session.client = None
        session.channel = None
        session.ssh_shell = None
        session.telnet_reader = None
        session.telnet_writer = None
        session.web_client = None

    async def _cleanup_loop(self):
        """Periodic cleanup of expired sessions."""
        interval = self.config.get('session', {}).get('cleanup_interval', 300)
        timeout = self.config.get('session', {}).get('timeout', 3600)

        while True:
            try:
                await asyncio.sleep(interval)
                await self._cleanup_expired(timeout)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")

    async def _cleanup_expired(self, timeout: int):
        """Remove expired sessions."""
        expired = []

        async with self._lock:
            for session_id, session in self.sessions.items():
                if session.is_expired(timeout) and not session.clients:
                    expired.append(session_id)

        for session_id in expired:
            await self.remove_session(session_id)
            logger.info(f"Cleaned up expired session {session_id}")

    def get_stats(self) -> dict:
        """Get session manager statistics."""
        return {
            'total_sessions': len(self.sessions),
            'active_sessions': sum(1 for s in self.sessions.values() if s.connected),
            'connecting_sessions': sum(1 for s in self.sessions.values() if s.connecting),
            'sessions_with_clients': sum(1 for s in self.sessions.values() if s.clients),
            'total_clients': sum(len(s.clients) for s in self.sessions.values())
        }
