import http.client
import socket
import ssl
from contextlib import ExitStack

from app.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    hostname = settings.allowed_hosts[0]
    with ExitStack() as resources:
        connection = resources.enter_context(
            socket.create_connection(("127.0.0.1", 8443), timeout=5)
        )
        if settings.secure:
            assert settings.certificate is not None
            context = ssl.create_default_context(cafile=str(settings.certificate))
            context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
            connection = resources.enter_context(
                context.wrap_socket(connection, server_hostname=hostname)
            )
        connection.sendall(
            f"GET /healthz HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n".encode(
                "ascii"
            )
        )
        response = resources.enter_context(http.client.HTTPResponse(connection))
        response.begin()
        if response.status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
