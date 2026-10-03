import http.client
import os
import socket
import ssl


def main() -> None:
    hostname = os.environ["APP_ALLOWED_HOSTS"].split(",")[0].strip()
    context = ssl.create_default_context(cafile=os.environ["APP_TLS_CERT_FILE"])
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    with socket.create_connection(("127.0.0.1", 8443), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=hostname) as connection:
            connection.sendall(
                f"GET /healthz HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n".encode(
                    "ascii"
                )
            )
            response = http.client.HTTPResponse(connection)
            response.begin()
            if response.status != 200:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
