"""Real HTTP/Waitress smoke test, with no hardware connection."""
import http.cookiejar
import re
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from waitress import create_server
from werkzeug.security import generate_password_hash

from test_midi import MidiGPIO
from test_power import main


class ServerTests(unittest.TestCase):
    def test_waitress_login_static_assets_and_hardware_readiness_over_http(self):
        main.pi = MidiGPIO()
        main.runtime_ready = False
        main.output_controller = main.create_output_controller()
        password = 'test-only-loopback-password'
        config = {'TESTING': False, 'AUTH_DISABLED': False,
                  'PASSWORD_HASH': generate_password_hash(password, method='pbkdf2:sha256:1000'),
                  'SECRET_KEY': 'test-only-key-' * 8, 'SESSION_COOKIE_SECURE': False,
                  'PUBLIC_ORIGIN': None}
        with patch.dict(main.app.config, config), self.assertNoLogs('waitress', level='ERROR'):
            server = create_server(main.app, host='127.0.0.1', port=0, threads=2)
            worker = threading.Thread(target=server.run, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.effective_port}'
            # This handler is only for the explicit loopback test server.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def request(path, data=None, headers=None):
                values = {'Connection': 'close'}
                values.update(headers or {})
                return opener.open(urllib.request.Request(base + path, data=data, headers=values), timeout=3)
            try:
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    request('/output_status')
                self.assertEqual(denied.exception.code, 401)
                denied.exception.close()
                with request('/login') as response:
                    page = response.read().decode()
                token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
                payload = urllib.parse.urlencode({'password': password, 'csrf_token': token}).encode()
                with request('/login', payload) as response:
                    page = response.read().decode()
                self.assertIn('masterPowerSwitch', page)
                token = re.search(r'name="csrf-token" content="([^"]+)"', page)[1]
                with request('/static/script.js') as response:
                    self.assertIn(b'X-CSRF-Token', response.read())
                with self.assertRaises(urllib.error.HTTPError) as unavailable:
                    request('/set_burst', b'bps=30&t_on=100', {
                        'X-CSRF-Token': token, 'X-Control-Tab': '00000000-0000-4000-8000-000000000001'})
                self.assertEqual(unavailable.exception.code, 503)
                unavailable.exception.close()
                self.assertEqual(main.pi.calls, [])
                self.assertEqual(main.pi.pwm_calls, [])
            finally:
                server.accepting = False
                server.task_dispatcher.shutdown(cancel_pending=True, timeout=5)
                server.close()
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
