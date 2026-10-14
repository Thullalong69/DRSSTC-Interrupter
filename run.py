"""Explicit, single-process production entrypoint. Never imported to start GPIO."""
import argparse
import os
import signal
from urllib.parse import urlsplit


def main(application=None, server_factory=None, argv=None):
    parser = argparse.ArgumentParser(description='DRSSTC-Steuerung starten')
    parser.add_argument('--credentials', default=os.environ.get('DRSSTC_CREDENTIALS', '/var/lib/drsstc/auth.json'))
    parser.add_argument('--lock-file', default=os.environ.get('DRSSTC_LOCK_FILE', '/run/drsstc/controller.lock'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--public-origin', default=os.environ.get('DRSSTC_PUBLIC_ORIGIN'))
    parser.add_argument('--allow-local-http', action='store_true', help='Nur für Zugriff über Loopback, ohne TLS')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('Ungültiger Port')
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('Der Dienst muss hinter einem TLS-Proxy auf Loopback lauschen')
    if args.public_origin:
        origin = urlsplit(args.public_origin)
        if (args.allow_local_http or origin.scheme != 'https' or not origin.hostname
            or origin.username or origin.password or origin.path or origin.query or origin.fragment):
            parser.error('Public Origin muss eine HTTPS-Origin ohne Pfad sein')
    elif not args.allow_local_http:
        parser.error('TLS Public Origin konfigurieren oder explizit --allow-local-http verwenden')
    if application is None:
        import main as application
    if server_factory is None:
        from waitress import create_server
        server_factory = create_server
    # Configuration errors fail before any GPIO connection or write.
    application.configure_auth(args.credentials, public_origin=args.public_origin)
    application.app.config['SESSION_COOKIE_SECURE'] = not args.allow_local_http
    application.app.config['TRUSTED_HOSTS'] = [
        urlsplit(args.public_origin).hostname if args.public_origin else '127.0.0.1', '127.0.0.1', 'localhost', '::1',
    ]
    server = None
    previous_handlers = {}
    try:
        application.initialize_hardware(args.lock_file)
        server = server_factory(application.app, host=args.host, port=args.port, threads=4,
                                connection_limit=100, max_request_body_size=16 * 1024)
        def terminate(signum, frame):
            raise SystemExit(0)
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, terminate)
        server.run()
    finally:
        try:
            application.shutdown_hardware()
        finally:
            try:
                if server is not None:
                    server.accepting = False
                    server.task_dispatcher.shutdown(cancel_pending=True, timeout=5)
                    server.close()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)


if __name__ == '__main__':
    main()
