"""Authentication, CSRF, and operator heartbeat integration tests."""
import itertools
import unittest
import uuid
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from control_lease import ControlLease
from test_midi import MidiGPIO
from test_power import main

_clock_ids = itertools.count(1)
_password = 'test-only-password-123'
_hash = generate_password_hash(_password, method='pbkdf2:sha256:1000')


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.clock = next(_clock_ids) * 1000
        self.timer = patch('security.time.monotonic', side_effect=lambda: self.clock)
        self.timer.start()
        self.config = patch.dict(main.app.config, {
            'TESTING': True, 'AUTH_DISABLED': False, 'PASSWORD_HASH': _hash,
            'SECRET_KEY': 'test-only-session-key-' * 4, 'SESSION_COOKIE_SECURE': True,
            'PUBLIC_ORIGIN': None,
        })
        self.config.start()
        main.pi = MidiGPIO()
        main.output_controller = main.create_output_controller()
        main.control_lease = ControlLease(lambda reason: main.safe_power_off(reason, preserve_fault=True), clock=lambda: self.clock)
        main.runtime_ready = True
        main.power_cancel = None
        main.softstart_active = False
        main.last_pulse_finished_at = None
        main.last_pulse_t_on = 0
        self.client = main.app.test_client()
        self.tabs = {}

    def tearDown(self):
        main.pi.fail_pin = None
        main.control_lease.power_off('Test beendet')
        main.runtime_ready = False
        self.config.stop()
        self.timer.stop()

    def csrf(self, client=None):
        client = client or self.client
        with client.session_transaction() as session:
            return session['csrf_token']

    def login(self, client=None):
        client = client or self.client
        client.get('/login', base_url='https://localhost')
        response = client.post('/login', base_url='https://localhost',
                               data={'password': _password, 'csrf_token': self.csrf(client)})
        self.assertEqual(response.status_code, 302)
        return client

    def post(self, path, client=None, **kwargs):
        client = client or self.client
        tab = self.tabs.setdefault(id(client), uuid.uuid4().hex)
        headers = {'X-CSRF-Token': self.csrf(client), 'X-Control-Tab': tab}
        headers.update(kwargs.pop('headers', {}))
        return client.post(path, base_url='https://localhost', headers=headers, **kwargs)

    def test_unauthenticated_commands_and_status_cannot_touch_gpio(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        for path in ('/set_burst', '/single_shot', '/toggle_power', '/stop_output'):
            response = self.client.post(path, data={'t_on': 100, 'bps': 30})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn('output', response.json)
        self.assertEqual(self.client.get('/output_status').status_code, 401)
        self.assertEqual(main.pi.pwm_calls, [])
        self.assertEqual(main.pi.calls, [])

    def test_missing_auth_configuration_fails_closed(self):
        with patch.dict(main.app.config, PASSWORD_HASH=None):
            self.assertEqual(self.client.post('/single_shot', data={'t_on': 100}).status_code, 503)
        self.assertEqual(main.pi.calls, [])

    def test_testing_bypass_is_not_available_when_testing_false(self):
        with patch.dict(main.app.config, TESTING=False, AUTH_DISABLED=True):
            self.assertEqual(self.client.post('/stop_output').status_code, 401)

    def test_login_rotates_csrf_and_cookie_has_secure_flags(self):
        response = self.client.get('/login', base_url='https://localhost')
        cookie = response.headers['Set-Cookie']
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        old_token = self.csrf()
        self.login()
        self.assertNotEqual(old_token, self.csrf())
        self.assertEqual(self.client.get('/', base_url='https://localhost').status_code, 200)

    def test_missing_or_wrong_csrf_and_foreign_origin_are_rejected(self):
        self.login()
        self.assertEqual(self.client.post('/single_shot', data={'t_on': 100}).status_code, 403)
        self.assertEqual(self.post('/single_shot', data={'t_on': 100}, headers={'X-CSRF-Token': 'wrong'}).status_code, 403)
        self.assertEqual(self.post('/single_shot', data={'t_on': 100}, headers={'Origin': 'https://attacker.invalid'}).status_code, 403)
        self.assertEqual(main.pi.calls, [])

    def test_forged_cookie_is_not_authenticated(self):
        self.client.set_cookie('session', 'not-a-signed-session')
        self.assertEqual(self.client.post('/stop_output').status_code, 401)

    def test_login_attempts_are_rate_limited(self):
        self.client.get('/login')
        for _ in range(5):
            response = self.client.post('/login', data={'password': 'wrong', 'csrf_token': self.csrf()})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.post('/login', data={'password': 'wrong', 'csrf_token': self.csrf()}).status_code, 429)

    def test_valid_login_and_command_claims_operator_lease(self):
        self.login()
        response = self.post('/set_burst', data={'bps': 30, 't_on': 100}, headers={'Origin': 'https://localhost'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['output']['mode'], 'burst')
        with self.client.session_transaction() as session:
            self.assertEqual(main.control_lease.owner, (session['operator_id'], self.tabs[id(self.client)]))

    def test_other_tab_with_same_login_cannot_renew_active_tab(self):
        self.login()
        self.post('/set_burst', data={'bps': 30, 't_on': 100})
        deadline = main.control_lease.deadline
        self.clock += 1
        response = self.post('/heartbeat', headers={'X-Control-Tab': uuid.uuid4().hex})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(main.control_lease.deadline, deadline)
        self.clock += 5
        self.assertTrue(main.control_lease.check_expired())
        self.assertEqual(main.output_controller.status()['mode'], 'idle')

    def test_other_browser_cannot_command_or_renew_owner(self):
        self.login()
        self.post('/set_burst', data={'bps': 30, 't_on': 100})
        deadline = main.control_lease.deadline
        other = self.login(main.app.test_client())
        self.clock += 1
        self.assertEqual(self.post('/set_burst', client=other, data={'bps': 10, 't_on': 100}).status_code, 409)
        self.assertEqual(self.post('/heartbeat', client=other).status_code, 409)
        self.assertEqual(main.control_lease.deadline, deadline)
        self.assertEqual(self.post('/heartbeat').status_code, 200)
        self.assertGreater(main.control_lease.deadline, deadline)
        self.assertEqual(self.post('/stop_output', client=other).status_code, 200)
        self.assertEqual(main.output_controller.status()['mode'], 'idle')

    def test_idle_heartbeat_does_not_arm_hardware(self):
        self.login()
        response = self.post('/heartbeat')
        self.assertFalse(response.json['controlling'])
        self.assertIsNone(main.control_lease.owner)
        self.assertEqual(main.pi.calls, [])

    def test_expired_heartbeat_cancels_softstart_and_all_outputs(self):
        self.login()
        with patch('threading.Thread.start'):
            self.post('/toggle_power', json={'power': True})
        softstart = main.power_cancel
        self.post('/set_burst', data={'bps': 30, 't_on': 100})
        self.clock += 6
        self.assertTrue(main.control_lease.check_expired())
        self.assertTrue(softstart.is_set())
        self.assertEqual(main.output_controller.status()['mode'], 'idle')
        self.assertEqual(main.pi.read(main.SOFTSTART_PIN), 1)
        self.assertEqual(main.pi.read(main.FULLPOWER_PIN), 1)
        self.assertIsNone(main.control_lease.owner)
        self.post('/heartbeat')
        self.assertEqual(main.output_controller.status()['mode'], 'idle')

    def test_owner_logout_powers_off_and_invalidates_session(self):
        self.login()
        self.post('/set_burst', data={'bps': 30, 't_on': 100})
        self.assertEqual(self.post('/logout').status_code, 200)
        self.assertIsNone(main.control_lease.owner)
        self.assertEqual(main.output_controller.status()['mode'], 'idle')
        self.assertEqual(self.client.post('/stop_output').status_code, 401)

    def test_nonowner_logout_does_not_interrupt_owner(self):
        self.login()
        self.post('/set_burst', data={'bps': 30, 't_on': 100})
        other = self.login(main.app.test_client())
        self.post('/logout', client=other)
        self.assertEqual(main.output_controller.status()['mode'], 'burst')

    def test_hardware_not_ready_rejects_commands(self):
        self.login()
        main.runtime_ready = False
        self.assertEqual(self.post('/set_burst', data={'bps': 30, 't_on': 100}).status_code, 503)
        self.assertEqual(main.pi.calls, [])

    def test_production_output_fault_attempts_power_off_and_remains_visible(self):
        self.login()
        original = main.pi.hardware_PWM
        def fail_active(pin, frequency, duty):
            if frequency:
                raise RuntimeError('simulated PWM failure')
            return original(pin, frequency, duty)
        with patch.object(main.pi, 'hardware_PWM', fail_active), self.assertLogs(main.logger, level='ERROR'):
            response = self.post('/set_burst', data={'bps': 30, 't_on': 100})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json['output']['mode'], 'fault')
        self.assertEqual(main.pi.read(main.SOFTSTART_PIN), 1)
        self.assertEqual(main.pi.read(main.FULLPOWER_PIN), 1)
        self.assertIsNone(main.control_lease.owner)

    def test_csp_allows_local_assets_and_no_inline_handlers(self):
        self.login()
        response = self.client.get('/')
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        page = response.get_data(as_text=True)
        self.assertNotIn('onclick=', page)
        self.assertNotIn('oninput=', page)
        self.assertNotIn('https://cdn', page)

    def test_oversized_request_is_rejected_before_gpio(self):
        self.login()
        response = self.post('/single_shot', data={'t_on': '1' * (20 * 1024)})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(main.pi.calls, [])


class LeaseTests(unittest.TestCase):
    def test_failed_shutdown_is_retried_and_cannot_be_renewed(self):
        calls = []
        now = [0]
        def stop(reason):
            calls.append(reason)
            if len(calls) == 1:
                raise RuntimeError('GPIO unavailable')
        lease = ControlLease(stop, clock=lambda: now[0])
        with lease.command('operator'):
            pass
        now[0] = 6
        with self.assertRaises(RuntimeError):
            lease.check_expired()
        self.assertTrue(lease.shutdown_pending)
        self.assertIsNone(lease.owner)
        self.assertTrue(lease.check_expired())
        self.assertFalse(lease.shutdown_pending)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
