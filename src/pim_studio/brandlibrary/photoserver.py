"""The photo server — our own folder, reached over FTP.

Small on purpose. It exists as its own module so `publish.py` can be tested
against a stand-in: the upload logic is where the expensive mistakes live (a
filename collision cost the first run 228 uploads) and none of them need a
server to demonstrate.

Three things here are inherited from the original app rather than chosen:

**TLS is negotiated, and verified by default.** `auto` tries FTPS and falls
back to plain FTP; `required` refuses to fall back; `off` never tries. And
certificates are checked unless `FTP_TLS_VERIFY` says otherwise — which is what
the downloader app has always done, and what its `.env` on the machine that ran
it is set to. An earlier version of this module disabled verification
unconditionally, on the belief that it was copying that app. It was copying the
throwaway script instead, and quietly weakening a connection that works fine
with verification on. Turn it off only if a TLS-inspecting proxy actually
breaks it, and know that you are.

**The connection is re-established mid-run.** A long upload session gets
dropped by the server, and the original script learned to reconnect and retry
the file rather than lose the remaining hour of work.

**Transfers are binary.** `storbinary` sets that per command, but `SIZE` — how
a resumed run asks whether a file is already there at the right length — is
refused in ASCII mode by some servers, so the mode is set once at connect.
"""

from __future__ import annotations

import contextlib
import ftplib
import ssl
from pathlib import Path
from typing import Protocol

from ..logging import log


class Uploader(Protocol):
    """What `publish` needs from a photo server, and nothing more."""

    def existing(self) -> set[str]:
        """Filenames already in the target directory."""

    def upload(self, local: Path, remote: str) -> int:
        """Send one file. Returns bytes sent."""

    def close(self) -> None: ...


class PhotoServer:
    """FTP-over-TLS to the folder this module owns."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 21,
        remote_dir: str = "pim-studio",
        timeout: int = 60,
        tls: str = "auto",
        verify: bool = True,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        # An empty value means the account root, which for this module would
        # scatter every photo beside a year of somebody else's files and break
        # every URL already published. The module owns a folder; falling back
        # to the default is safer than honouring a blank.
        self.remote_dir = (remote_dir or "pim-studio").strip("/")
        self.timeout = timeout
        self.tls = (tls or "auto").lower()
        self.verify = verify
        self._ftp: ftplib.FTP | None = None

    def _login(self, ftp: ftplib.FTP) -> None:
        ftp.connect(self.host, self.port, self.timeout)
        ftp.login(self.user, self.password)
        if isinstance(ftp, ftplib.FTP_TLS):
            # Encrypt the data channel too — without this the files themselves
            # go in clear text even though the credentials did not.
            ftp.prot_p()
        # See the docstring: SIZE, which the resume check uses, is refused in
        # ASCII mode by some servers.
        ftp.voidcmd("TYPE I")
        # Already there is the normal case, and the only other reason this
        # fails is a permission problem `cwd` is about to report better.
        with contextlib.suppress(ftplib.error_perm):
            ftp.mkd(self.remote_dir)
        ftp.cwd(self.remote_dir)

    def _connect(self) -> ftplib.FTP:
        if self.tls in ("auto", "required"):
            context = ssl.create_default_context()
            if not self.verify:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            secure = ftplib.FTP_TLS(context=context)
            try:
                self._login(secure)
                return secure
            except Exception:
                if self.tls == "required":
                    raise
                # `auto` means try TLS and accept plain if the server or a
                # middlebox will not do it. Logged, because a run silently
                # sending a password in clear text is worth knowing about.
                log.warning("photoserver_tls_unavailable", host=self.host, falling_back="plain")

        plain = ftplib.FTP()
        self._login(plain)
        return plain

    @property
    def ftp(self) -> ftplib.FTP:
        if self._ftp is None:
            self._ftp = self._connect()
        return self._ftp

    def existing(self) -> set[str]:
        """Everything already published, read once.

        This is the resume state, and reading it once up front is what makes an
        interrupted run cheap to restart — which matters because a pasted
        cookie expires within hours and a full run is longer than that.
        """
        try:
            names = set(self.ftp.nlst())
        except ftplib.error_perm as exc:
            # An empty directory answers 550 on some servers rather than an
            # empty list. That is not a failure, and treating it as one would
            # refuse to publish into a fresh folder.
            if str(exc).startswith("550"):
                return set()
            raise
        log.info("photoserver_listed", directory=self.remote_dir, files=len(names))
        return names

    def upload(self, local: Path, remote: str) -> int:
        size = local.stat().st_size
        with local.open("rb") as handle:
            try:
                self.ftp.storbinary(f"STOR {remote}", handle)
            except (ftplib.error_temp, OSError):
                # The session was dropped. Reconnect and send the file again
                # rather than lose the rest of the run.
                log.warning("photoserver_reconnecting", remote=remote)
                self.close()
                handle.seek(0)
                self.ftp.storbinary(f"STOR {remote}", handle)
        return size

    def close(self) -> None:
        if self._ftp is None:
            return
        try:
            self._ftp.quit()
        except Exception:  # noqa: BLE001 — a close that fails is still closed
            pass
        finally:
            self._ftp = None
