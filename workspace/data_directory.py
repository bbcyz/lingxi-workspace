"""Central per-user data directory and verified, non-destructive legacy migration."""
from datetime import datetime
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


class DataDirectoryError(RuntimeError):
    pass


def database_digest(connection):
    digest=hashlib.sha256()
    for line in connection.iterdump():
        digest.update(line.encode('utf-8'));digest.update(b'\n')
    return digest.hexdigest()


def migrate_data(source, destination, *, recharge_source=None, private_files=()):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if source==destination or source.is_relative_to(destination):
        raise DataDirectoryError('新数据目录不能覆盖旧目录或使用其上级目录。')
    original=source/'accounts.sqlite3';target=destination/'accounts.sqlite3'
    if not original.is_file():raise DataDirectoryError('旧数据目录没有账号数据库。')
    if target.exists():raise DataDirectoryError('目标已有账号数据库，未覆盖或合并。')
    if (source/'desktop-instance.json').exists():raise DataDirectoryError('旧客户端仍有运行记录，请先正常退出。')
    folders=[]
    if recharge_source is not None and Path(recharge_source).is_dir():
        folders.append((Path(recharge_source).resolve(),destination/'recharge'))
    for name in ('activity-plugins','window-profile','member-benefits','mobile-rights'):
        if (source/name).is_dir():folders.append((source/name,destination/name))
    for old,new in folders:
        if old==source or new.resolve().is_relative_to(old):
            raise DataDirectoryError('运行数据目录不能移动到自身内部。')
        if new.exists():raise DataDirectoryError('目标运行目录已存在，未覆盖：'+new.name)
    private=[]
    for file in private_files:
        file=Path(file).resolve()
        if not file.is_relative_to(source):raise DataDirectoryError('凭据备份不在指定的旧数据目录中。')
        private.append((file,destination/'private-backups'/file.relative_to(source)))
    if any(new.exists() for _,new in private):raise DataDirectoryError('目标已有同名凭据备份，未覆盖。')
    destination.mkdir(parents=True,exist_ok=True)
    temporary=destination/('accounts.'+uuid.uuid4().hex+'.migrating')
    temporary.touch(exist_ok=False)
    with closing(sqlite3.connect(original.as_uri()+'?mode=ro',uri=True)) as old,closing(sqlite3.connect(temporary)) as new:
        old.backup(new)
        if new.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or database_digest(old)!=database_digest(new):
            raise DataDirectoryError('数据库核对未通过，原库保留，未切换数据目录。')
        tables={name:new.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]
                for (name,) in new.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    temporary.rename(target)
    backup=destination/'migration-backups'/datetime.now().strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True,exist_ok=False)
    # Every move stays inside the explicit source/destination trees checked
    # above. Originals are archived, not deleted; no account data is discarded.
    for name in ('accounts.sqlite3','accounts.sqlite3-wal','accounts.sqlite3-shm','desktop.lock',
                 'desktop.log','desktop.log.1','desktop.log.2'):
        file=source/name
        if file.exists():file.rename(backup/name)
    for old,new in folders:
        if not new.resolve().is_relative_to(destination):raise DataDirectoryError('运行数据目标超出指定目录。')
        old.rename(new)
    moved=[]
    for old,new in private:
        new.parent.mkdir(parents=True,exist_ok=True)
        before=hashlib.sha256(old.read_bytes()).hexdigest()
        old.rename(new)
        if hashlib.sha256(new.read_bytes()).hexdigest()!=before:raise DataDirectoryError('凭据备份移动后的校验不一致。')
        moved.append({'file':str(new.relative_to(destination)),'sha256':before})
    report={'source':str(source),'destination':str(destination),'tables':tables,
            'recharge_migrated':any(new.name=='recharge' for _,new in folders),'private_files':moved,
            'original_database_backup':str(backup/'accounts.sqlite3')}
    (backup/'migration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def prepare_default_data(root, destination, legacy_client):
    """Only the default runtime migrates; explicit test/instance dirs stay isolated."""
    root=Path(root).resolve();destination=Path(destination).resolve();legacy_client=Path(legacy_client).resolve()
    if (destination/'accounts.sqlite3').exists():return
    candidates=[]
    for folder in (root/'.local',legacy_client):
        database=folder/'accounts.sqlite3'
        if not database.exists():continue
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
            tables={name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            account_tables={'account_sessions','activity_accounts','credential_sessions','activity_state',
                            'lingxi_tasks','checkin_data','monthly_coupon_data','star_day_data','coupon_use_data'}
            populated=any(db.execute('SELECT 1 FROM "'+name+'" LIMIT 1').fetchone() for name in account_tables&tables)
        candidates.append((folder,populated))
    populated=[folder for folder,has_data in candidates if has_data]
    if len(populated)>1:raise DataDirectoryError('发现两个非空旧账号库，请先明确需要迁移的来源，不自动混合账号。')
    source=populated[0] if populated else candidates[0][0] if candidates else None
    if source is not None:
        migrate_data(source,destination,recharge_source=root/'recharge/.local' if source==root/'.local' else source/'recharge')
    else:destination.mkdir(parents=True,exist_ok=True)
