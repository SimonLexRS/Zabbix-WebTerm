"""
Telnet Client handler using telnetlib3 for WebTerm proxy.
"""

import asyncio
import logging
import telnetlib3

logger = logging.getLogger(__name__)


class TelnetClientHandler:
    """Handles Telnet connections using telnetlib3."""

    def __init__(self, session):
        self.session = session
        self._read_task = None
        self._buffer = ""

    async def connect(self, host: str, port: int):
        """Establish Telnet connection."""
        try:
            # Open Telnet connection
            reader, writer = await telnetlib3.open_connection(
                host=host,
                port=port,
                encoding='utf-8',
                encoding_errors='replace',
                connect_minwait=0.0
            )

            # Store connection objects
            self.session.telnet_reader = reader
            self.session.telnet_writer = writer
            self.session.connected = True
            self.session.connecting = False

            logger.info(f"Telnet connected to {host}:{port} for session {self.session.session_id}")

            # Start reading output
            self._read_task = asyncio.create_task(self._read_loop())

            return True

        except ConnectionRefusedError:
            logger.warning(f"Telnet connection refused for {host}:{port}")
            self.session.connecting = False
            raise Exception("Connection refused")

        except asyncio.TimeoutError:
            logger.warning(f"Telnet connection timeout for {host}:{port}")
            self.session.connecting = False
            raise Exception("Connection timeout")

        except OSError as e:
            logger.warning(f"Telnet connection error for {host}:{port}: {e}")
            self.session.connecting = False
            raise Exception(f"Connection error: {e}")

        except Exception as e:
            logger.error(f"Telnet connection error for {host}:{port}: {e}")
            self.session.connecting = False
            raise Exception(f"Connection failed: {str(e)}")

    async def _read_loop(self):
        """Read output from Telnet and broadcast to clients."""
        try:
            while self.session.connected and self.session.telnet_reader:
                try:
                    # Read with timeout to allow checking for disconnect
                    data = await asyncio.wait_for(
                        self.session.telnet_reader.read(4096),
                        timeout=0.1
                    )

                    if data:
                        await self.session.broadcast(data)
                        self.session.update_activity()
                    else:
                        # Connection closed
                        break

                except asyncio.TimeoutError:
                    # Normal timeout, continue loop
                    continue

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Telnet read error: {e}")
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
            if self.session.telnet_writer:
                self.session.telnet_writer.close()
                await self.session.telnet_writer.wait_closed()
        except Exception as e:
            logger.warning(f"Error closing Telnet: {e}")

        self.session.telnet_reader = None
        self.session.telnet_writer = None

        logger.info(f"Telnet session {self.session.session_id} disconnected")

    async def disconnect(self):
        """Disconnect Telnet session."""
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        await self._handle_disconnect()
