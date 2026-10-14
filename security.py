"""Session authentication and CSRF protection for the control interface."""
import secrets
import threading
import time
from collections import deque
from datetime import timedelta

from flask import jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash


def install_security(app, on_logout):
    app.config.update(
        SECRET_KEY=secrets.token_hex(32), PASSWORD_HASH=None, AUTH_DISABLED=False,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=True, PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    attempts = deque(maxlen=100)
    attempts_lock = threading.Lock()

    def testing_bypass():
        return app.testing and app.config['AUTH_DISABLED']

    def csrf_valid():
        expected = session.get('csrf_token', '')
        supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
        if not expected or not isinstance(supplied, str):
            return False
        origin = request.headers.get('Origin')
        allowed = app.config.get('PUBLIC_ORIGIN') or request.host_url.rstrip('/')
        if origin and origin != allowed:
            return False
        return secrets.compare_digest(expected, supplied)

    @app.before_request
    def check_access():
        if testing_bypass() or request.endpoint in ('static', 'login', 'health'):
            return None
        if not app.config['PASSWORD_HASH']:
            return jsonify(status='error', message='Anmeldung ist noch nicht konfiguriert'), 503
        if not session.get('authenticated'):
            if request.endpoint == 'index':
                return redirect(url_for('login'))
            return jsonify(status='error', message='Anmeldung erforderlich'), 401
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not csrf_valid():
            return jsonify(status='error', message='Ungültiger CSRF-Token oder Ursprung'), 403

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        error = None
        status = 200
        if request.method == 'POST':
            if not app.config['PASSWORD_HASH']:
                error, status = 'Anmeldung ist noch nicht konfiguriert', 503
            elif not csrf_valid():
                error, status = 'Ungültige Anmeldung; Seite neu laden', 403
            else:
                now = time.monotonic()
                address = request.remote_addr
                with attempts_lock:
                    while attempts and attempts[0][0] < now - 60:
                        attempts.popleft()
                    limited = len(attempts) >= 20 or sum(ip == address for _, ip in attempts) >= 5
                    if not limited:
                        attempts.append((now, address))
                if limited:
                    error, status = 'Zu viele Anmeldeversuche; später erneut versuchen', 429
                elif check_password_hash(app.config['PASSWORD_HASH'], request.form.get('password', '')):
                    session.clear()
                    session.update(authenticated=True, operator_id=secrets.token_hex(16),
                                   csrf_token=secrets.token_urlsafe(32))
                    session.permanent = True
                    return redirect(url_for('index'))
                else:
                    error, status = 'Anmeldung fehlgeschlagen', 401
        return render_template('login.html', csrf_token=session['csrf_token'], error=error), status

    @app.route('/logout', methods=['POST'])
    def logout():
        try:
            on_logout(session.get('operator_id'))
        finally:
            session.clear()
        return jsonify(status='success', message='Abgemeldet')

    @app.after_request
    def security_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response
