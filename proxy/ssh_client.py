"""
SSH Client handler using paramiko for WebTerm proxy.
"""

import asyncio
import logging
import paramiko
from paramiko.ssh_exception import SSHException, AuthenticationException

logger = logging.getLogger(__name__)


class SSHClientHandler:
    """Handles SSH connections using paramiko."""

    def __init__(self, session):
        self.session = session
        self._read_task = None

    async def connect(self, host: str, port: int, username: str, password: str):
        """Establish SSH connection."""
        try:
            # Create SSH client
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            # Connect in executor to avoid blocking
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: client.connect(
                    hostname=host,
                    port=port,
                    username=username,
                    password=password,
                    look_for_keys=False,
                    allow_agent=False,
                    timeout=30
                )
            )

            # Open interactive shell session
            transport = client.get_transport()

            # Enable keep-alive to prevent connection timeout
            if transport:
                transport.set_keepalive(30)  # Send keepalive every 30 seconds

            channel = transport.open_session()
            channel.get_pty(term='xterm-256color', width=80, height=24)
            channel.invoke_shell()

            # Store connection objects
            self.session.client = client
            self.session.channel = channel
            self.session.ssh_shell = channel
            self.session.connected = True
            self.session.connecting = False

            logger.info(f"SSH connected to {host}:{port} for session {self.session.session_id}")

            # Start reading output
            self._read_task = asyncio.create_task(self._read_loop())

            return True

        except AuthenticationException as e:
            logger.warning(f"SSH authentication failed for {host}:{port}: {e}")
            self.session.connecting = False
            raise Exception(f"Authentication failed: {str(e)}")

        except SSHException as e:
            logger.warning(f"SSH error for {host}:{port}: {e}")
            self.session.connecting = False
            raise Exception(f"SSH error: {str(e)}")

        except Exception as e:
            logger.error(f"SSH connection error for {host}:{port}: {e}")
            self.session.connecting = False
            raise Exception(f"Connection failed: {str(e)}")

    async def _read_loop(self):
        """Read output from SSH channel and broadcast to clients."""
        try:
            while self.session.connected and self.session.ssh_shell:
                try:
                    # Check if channel is closed before reading
                    if self.session.ssh_shell.closed:
                        break

                    # Check if data is available (non-blocking)
                    if self.session.ssh_shell.recv_ready():
                        data = self.session.ssh_shell.recv(4096)
                        if data:
                            # Decode and broadcast
                            text = data.decode('utf-8', errors='replace')
                            await self.session.broadcast(text)
                            self.session.update_activity()
                        else:
                            # Connection closed cleanly
                            break
                    else:
                        # Small delay to prevent CPU spinning
                        await asyncio.sleep(0.01)

                except (SSHException, EOFError, OSError) as e:
                    logger.warning(f"SSH channel error for session {self.session.session_id}: {e}")
                    break
                except Exception as e:
                    logger.error(f"Unexpected SSH read error for session {self.session.session_id}: {e}")
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"SSH read loop error for session {self.session.session_id}: {e}")
        finally:
            await self._handle_disconnect()

    async def _handle_disconnect(self):
        """Handle disconnection."""
        if not self.session.connected:
            return

        self.session.connected = False
        await self.session.broadcast("\r\n\x1b[31m[Connection closed]\x1b[0m\r\n")

        # Close connection
        try:
            if self.session.ssh_shell:
                self.session.ssh_shell.close()
            if self.session.client:
                self.session.client.close()
        except Exception as e:
            logger.warning(f"Error closing SSH: {e}")

        self.session.ssh_shell = None
        self.session.client = None

        logger.info(f"SSH session {self.session.session_id} disconnected")

    async def disconnect(self):
        """Disconnect SSH session."""
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        await self._handle_disconnect()
