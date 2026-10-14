"""Run the rendered page's JavaScript against a small simulated DOM."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_power import main


class InterfaceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for the UI simulation')
    def test_controller_status_stop_and_stale_responses(self):
        client = main.app.test_client()
        with client.session_transaction() as session:
            session['csrf_token'] = 'test-csrf-token'
        page = client.get('/')
        self.assertEqual(page.status_code, 200)
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_bytes(page.data)
            result = subprocess.run(
                [shutil.which('node'), str(Path(__file__).with_name('check_interface.js')), str(html),
                 str(Path(main.__file__).parent / 'static' / 'script.js')],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('UI simulation passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
