from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from workspace.data_directory import migrate_data,prepare_default_data,DataDirectoryError


class DataDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.source=self.root/'project/.local';self.target=self.root/'user/data'
        self.source.mkdir(parents=True)

    def database(self,folder,account='existing'):
        folder.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(folder/'accounts.sqlite3')) as db:
            db.execute('CREATE TABLE account_sessions(login_id TEXT PRIMARY KEY,payload BLOB)')
            db.execute('CREATE TABLE credential_sessions(scope TEXT,login_id TEXT,payload BLOB)')
            db.execute('CREATE TABLE tasks(id TEXT,payload TEXT)')
            if account:
                db.execute('INSERT INTO account_sessions VALUES(?,?)',(account,b'opaque-encrypted-original'))
                db.execute('INSERT INTO credential_sessions VALUES(?,?,?)',('partner',account,b'opaque-partner-session'))
            db.execute('INSERT INTO tasks VALUES(?,?)',('scheduled','{"clock":"08:00:00","burst_count":40,"burst_interval":0.05}'))
            db.commit()

    def test_migrates_accounts_credentials_schedules_and_private_backups_without_loss(self):
        self.database(self.source)
        private=self.source/'audit/account.dpapi';private.parent.mkdir();private.write_bytes(b'encrypted-private-evidence')
        recharge=self.root/'project/recharge/.local';recharge.mkdir(parents=True)
        (recharge/'schedules.json').write_text('{"unchanged":true}')
        for name in ('member-benefits','mobile-rights'):
            (self.source/name).mkdir();(self.source/name/'private-state.json').write_text('{"unchanged":true}')
        report=migrate_data(self.source,self.target,recharge_source=recharge,private_files=[private])
        with closing(sqlite3.connect(self.target/'accounts.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT * FROM account_sessions').fetchall(),[('existing',b'opaque-encrypted-original')])
            self.assertEqual(db.execute('SELECT payload FROM credential_sessions').fetchone()[0],b'opaque-partner-session')
            self.assertEqual(json.loads(db.execute('SELECT payload FROM tasks').fetchone()[0])['burst_interval'],.05)
        self.assertFalse((self.source/'accounts.sqlite3').exists());self.assertTrue(Path(report['original_database_backup']).is_file())
        self.assertEqual((self.target/'recharge/schedules.json').read_text(),'{"unchanged":true}')
        self.assertEqual((self.target/'private-backups/audit/account.dpapi').read_bytes(),b'encrypted-private-evidence')
        for name in ('member-benefits','mobile-rights'):
            self.assertFalse((self.source/name).exists())
            self.assertEqual((self.target/name/'private-state.json').read_text(),'{"unchanged":true}')
        self.assertFalse(private.exists())

    def test_existing_target_is_not_overwritten(self):
        self.database(self.source,'old');self.database(self.target,'new')
        before=(self.target/'accounts.sqlite3').read_bytes()
        with self.assertRaises(DataDirectoryError):migrate_data(self.source,self.target)
        self.assertEqual((self.target/'accounts.sqlite3').read_bytes(),before)
        self.assertTrue((self.source/'accounts.sqlite3').is_file())

    def test_running_client_stops_migration_before_copy(self):
        self.database(self.source);(self.source/'desktop-instance.json').write_text('{}')
        with self.assertRaises(DataDirectoryError):migrate_data(self.source,self.target)
        self.assertFalse((self.target/'accounts.sqlite3').exists())

    def test_old_packaged_directory_can_migrate_into_its_data_child(self):
        old=self.root/'user';self.database(old)
        report=migrate_data(old,old/'data')
        self.assertEqual(report['tables']['account_sessions'],1)
        self.assertTrue((old/'data/accounts.sqlite3').exists());self.assertFalse((old/'accounts.sqlite3').exists())

    def test_two_populated_legacy_databases_are_not_implicitly_merged(self):
        self.database(self.source,'developer');legacy=self.root/'old-client';self.database(legacy,'client')
        with self.assertRaises(DataDirectoryError):prepare_default_data(self.source.parent,self.target,legacy)
        self.assertTrue((self.source/'accounts.sqlite3').exists());self.assertTrue((legacy/'accounts.sqlite3').exists())

    def test_existing_new_directory_is_reused_without_importing_legacy_again(self):
        self.database(self.source,'old');self.database(self.target,'current')
        prepare_default_data(self.source.parent,self.target,self.root/'legacy')
        with closing(sqlite3.connect(self.target/'accounts.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT login_id FROM account_sessions').fetchone()[0],'current')

    def test_independent_activity_login_is_not_mistaken_for_an_empty_database(self):
        self.database(self.source,account=None);legacy=self.root/'old-client';self.database(legacy,account=None)
        with closing(sqlite3.connect(legacy/'accounts.sqlite3')) as db:
            db.execute('CREATE TABLE activity_state(platform_id TEXT,payload BLOB)')
            db.execute('INSERT INTO activity_state VALUES(?,?)',('wechat-checkin',b'opaque-independent-login'));db.commit()
        prepare_default_data(self.source.parent,self.target,legacy)
        with closing(sqlite3.connect(self.target/'accounts.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT payload FROM activity_state').fetchone()[0],b'opaque-independent-login')
        self.assertTrue((self.source/'accounts.sqlite3').exists())


if __name__=='__main__':unittest.main()
