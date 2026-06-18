from __future__ import annotations

import os
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .session import IxiaSession

from .models import IxiaOperationError


class ConfigManager:
    def __init__(self, session: IxiaSession) -> None:
        self._session = session

    def save(
        self,
        local_path: str,
        ssh_user: Optional[str] = None,
        ssh_password: Optional[str] = None,
        server_temp_dir: str = r"C:\temp",
    ) -> None:
        """Save current session config to a local .ixncfg file.

        Saves on the API server, then downloads via SFTP.
        """
        from ixnetwork_restpy import Files
        from ._discovery import _ssh_connect, _ssh_exec

        ixn = self._session.ixn
        host = self._session.host
        user = ssh_user or self._session.user
        password = ssh_password or self._session.password

        filename = os.path.basename(local_path)
        server_path = f"{server_temp_dir}\\{filename}"

        try:
            ssh = _ssh_connect(host, user, password)
            _ssh_exec(ssh, f'if not exist "{server_temp_dir}" mkdir "{server_temp_dir}"')

            ixn.SaveConfig(Files(server_path, local_file=False))

            sftp = ssh.open_sftp()
            try:
                os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
                sftp.get(server_path, local_path)
            finally:
                sftp.close()
                ssh.close()
        except IxiaOperationError:
            raise
        except Exception as e:
            raise IxiaOperationError(f"Failed to save config: {e}") from e

    def load(self, path: str, local_file: bool = True) -> None:
        """Load an .ixncfg configuration file into the session."""
        from ixnetwork_restpy import Files

        ixn = self._session.ixn
        try:
            ixn.LoadConfig(Files(path, local_file=local_file))
        except Exception as e:
            raise IxiaOperationError(f"Failed to load config {path}: {e}") from e

    def new(self) -> None:
        """Clear session config (blank slate). Ports remain assigned."""
        ixn = self._session.ixn
        try:
            ixn.NewConfig()
        except Exception as e:
            raise IxiaOperationError(f"Failed to clear config: {e}") from e

    def sessions(self) -> list[dict[str, Any]]:
        """List available IxNetwork sessions on the server."""
        sa = self._session._session
        results: list[dict[str, Any]] = []
        try:
            tp = getattr(sa, "TestPlatform", None)
            if tp is None:
                return results
            for sess in tp.Sessions.find():
                results.append({
                    "id": getattr(sess, "Id", None),
                    "name": getattr(sess, "Name", ""),
                    "state": getattr(sess, "State", ""),
                })
        except Exception as e:
            raise IxiaOperationError(f"Failed to list sessions: {e}") from e
        return results
