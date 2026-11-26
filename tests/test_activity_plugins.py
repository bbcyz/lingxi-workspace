from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile, ZipInfo

from app import create_app


PACKAGE_PATH = '/api/platforms/packages'
ACTIVITY = 'fixture-campaign'
HOME = '/' + ACTIVITY
ROOT = 'fixture_campaign/'
SOURCE = '''
from threading import RLock
from flask import Blueprint, jsonify, render_template
from .details import TITLE

class Controller:
    def __init__(self):
        self.deleted = False
        self.scheduler = self
        self.lock = RLock()

    def start(self): pass
    def stop(self, wait=True): pass
    def busy_login(self, login_id): return False
    def forget(self, login_id): pass
    def metadata(self): return {'active': True, 'ended': False}

def register(app, credentials, accounts, storage):
    bp = Blueprint('fixture_campaign', __name__, template_folder='templates', static_folder='static',
                   static_url_path='/activities/fixture-campaign/static')
    @bp.get('/fixture-campaign')
    def page(): return render_template('fixture_campaign.html', title=TITLE)
    @bp.get('/api/fixture-campaign/status')
    def status(): return jsonify(storage.load_activity_data('fixture-campaign'))
    app.register_blueprint(bp)
    return Controller()
'''


def make_archive(files):
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        for name, body in files.items():
            entry = ZipInfo(name)
            entry.filename = name  # Preserve a malformed member name when testing Windows separators.
            archive.writestr(entry, body)
    return output.getvalue()


def sample_archive(version='1.0.0', *, source=SOURCE, extra=None):
    files = {
        ROOT+'manifest.json': json.dumps({'format_version': 1, 'id': ACTIVITY, 'version': version,
            'title': '测试活动包', 'module': 'activities.fixture_campaign.plugin', 'home': HOME, 'temporary': False}),
        ROOT+'__init__.py': '', ROOT+'plugin.py': source,
        ROOT+'details.py': f'TITLE = {version!r}\n',
        ROOT+'templates/fixture_campaign.html': '<h1>{{ title }}</h1>',
        ROOT+'static/fixture.js': '// fixture asset',
    }
    return make_archive({**files, **(extra or {})})


class InstalledActivityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database_path = Path(folder.name)/'accounts.sqlite3'
        self.launch()

    def launch(self):
        self.app = create_app(database_path=self.database_path)
        self.client = self.app.test_client()
        self.manager = self.app.extensions['platform_manager']
        self.addCleanup(self.manager.shutdown)

    def upload(self, data, **kwargs):
        return self.client.post(PACKAGE_PATH, data=data, content_type='application/zip', **kwargs)

    def row(self, activity=ACTIVITY):
        return next(row for row in self.client.get('/api/platforms').json['platforms'] if row['id'] == activity)

    def test_install_update_export_and_fallback_preserve_existing_state(self):
        self.assertEqual(self.upload(sample_archive()).status_code, 201)
        self.assertEqual(self.row()['pending_version'], '1.0.0')
        self.assertEqual(self.client.get(HOME).status_code, 404)
        with self.client.get('/api/platforms/fixture-campaign/package') as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn('fixture-campaign-1.0.0.zip', response.headers['Content-Disposition'])

        self.launch()
        self.assertEqual(self.row()['version'], '1.0.0')
        self.assertEqual(self.client.get(HOME).data, b'<h1>1.0.0</h1>')
        with self.client.get('/activities/fixture-campaign/static/fixture.js') as asset:
            self.assertEqual(asset.data, b'// fixture asset')
        stored = {'enable': True, 'jobs': {'account-1': ['record']}}
        self.manager.storage.save_activity_data('activity-plugin:'+ACTIVITY, stored)
        self.assertEqual(self.client.post('/api/features/'+ACTIVITY, json={'enabled': False}).status_code, 200)
        self.assertEqual(self.upload(sample_archive('1.0.1')).status_code, 201)
        self.assertEqual(self.row()['version'], '1.0.0')
        self.assertEqual(self.row()['pending_version'], '1.0.1')
        self.launch()
        self.assertFalse(self.row()['enabled'])
        self.assertEqual(self.client.get('/api/fixture-campaign/status').status_code, 423)
        self.client.post('/api/features/'+ACTIVITY, json={'enabled': True})
        self.assertEqual(self.client.get('/api/fixture-campaign/status').json, stored)
        self.assertEqual(self.client.get(HOME).data, b'<h1>1.0.1</h1>')
        self.assertEqual(self.upload(sample_archive('1.0.2', source=SOURCE.replace('return Controller()',
            "raise ValueError('fixture registration failure')"))).status_code, 201)
        with self.assertLogs(self.app.logger.name, level='WARNING'):
            self.launch()
        self.assertEqual(self.row()['failed_version'], '1.0.2')
        self.assertEqual(self.client.get(HOME).data, b'<h1>1.0.1</h1>')
        self.assertEqual(self.client.get('/api/fixture-campaign/status').json, stored)

    def test_explicitly_removed_package_can_be_reinstalled(self):
        self.assertEqual(self.upload(sample_archive()).status_code, 201)
        self.launch()
        storage = self.manager.storage
        storage.save_activity_data(ACTIVITY, {'host': True})
        storage.save_credentials('activity-plugin:'+ACTIVITY+':demo', 'account-1', {'test': True})
        self.assertEqual(self.client.post(f'/api/platforms/{ACTIVITY}/delete', json={'confirmation': ACTIVITY}).status_code, 200)
        self.assertEqual(self.row()['state'], 'deleted')
        self.assertEqual(self.client.get(HOME).status_code, 410)
        self.assertEqual(storage.load_activity_data(ACTIVITY), {'host': True})
        self.assertIsNone(storage.load_credentials('activity-plugin:'+ACTIVITY+':demo', 'account-1'))
        self.assertFalse((self.database_path.parent/'activity-plugins'/ACTIVITY).exists())
        self.assertEqual(self.upload(sample_archive()).status_code, 201)
        self.assertEqual(self.row()['state'], 'pending')
        self.launch()
        self.assertEqual(self.client.get(HOME).status_code, 200)

    def test_exports_of_all_bundled_activities_can_be_installed_as_updates(self):
        known = ('daily-gold', 'seven-day', 'wechat-checkin')
        for key in known:
            with self.subTest(activity=key), self.client.get(f'/api/platforms/{key}/package') as download:
                self.assertEqual(download.status_code, 200)
                self.assertEqual(self.upload(download.data).status_code, 409)
                with ZipFile(BytesIO(download.data)) as archive:
                    files = {name: archive.read(name) for name in archive.namelist()}
                self.assertFalse(any('/tests/' in name or '__pycache__' in name for name in files))
                manifest_path = next(name for name in files if name.endswith('/manifest.json'))
                manifest = json.loads(files[manifest_path])
                files[manifest_path] = json.dumps({**manifest, 'version': '1.0.1'})
                response = self.upload(make_archive(files))
                self.assertEqual(response.status_code, 201, response.json)
        self.launch()
        for key in known:
            with self.subTest(activity=key):
                row = self.row(key)
                self.assertEqual(row['version'], '1.0.1', row)
                self.assertEqual(self.client.get(row['home']).status_code, 200)
        with self.client.get('/activities/wechat-checkin/static/web-checkin.js') as asset:
            self.assertEqual(asset.status_code, 200)
        self.assertIn(b'href="/checkin/wechat"', self.client.get('/checkin').data)

    def test_host_conflicts_nonlocal_requests_and_invalid_archives_are_rejected(self):
        for path in (ROOT+'../elsewhere.py', ROOT+'config\\local.py', ROOT+'Plugin.py',
                     ROOT+'data/credentials.json', ROOT+'CON.py', 'alternate/__init__.py'):
            with self.subTest(path=path):
                response = self.upload(sample_archive(extra={path: ''}))
                self.assertEqual(response.status_code, 400, response.json)
        response = self.upload(sample_archive(source='def broken('))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.upload(sample_archive(extra={ROOT+'manifest.json': json.dumps({
            'format_version': 1, 'id': 'platforms', 'version': '1.0.0', 'title': '冲突入口',
            'module': 'activities.fixture_campaign.plugin', 'home': '/platforms', 'temporary': True,
        })})).status_code, 409)
        self.assertEqual(self.upload(sample_archive(), headers={'Origin': 'http://unrelated.example'}).status_code, 403)
        self.app.extensions['access_security'].set_password('test-package-password', '')
        remote = dict(base_url='http://192.168.1.8:5500', environ_overrides={'REMOTE_ADDR': '192.168.1.43'})
        self.assertEqual(self.client.post('/api/access/login', json={'password': 'test-package-password'}, **remote).status_code, 200)
        self.assertEqual(self.upload(sample_archive(), **remote).status_code, 403)
        self.assertEqual(self.client.get('/api/platforms/daily-gold/package', **remote).status_code, 403)
        self.assertNotIn(ACTIVITY, self.manager.releases)
