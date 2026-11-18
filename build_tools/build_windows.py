"""Build the Windows x64 portable client from an explicit resource allowlist."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runtime_paths import VERSION
from build_tools.activity_resources import stage_activities


def sha256(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    from PIL import Image
    assets=ROOT/'desktop_assets';assets.mkdir(exist_ok=True)
    image=Image.open(assets/'app.png').convert('RGBA')
    image.save(assets/'app.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
    shutil.copy2(assets/'app.ico',ROOT/'static/app.ico')
    notice_dir=ROOT/'.build/notices'
    if notice_dir.exists():
        if notice_dir.resolve().parent != (ROOT/'.build').resolve():
            raise RuntimeError('Notice staging path escaped the build directory')
        shutil.rmtree(notice_dir)
    notice_dir.mkdir()
    shutil.copy2(Path(sys.base_prefix)/'LICENSE.txt',notice_dir/'Python-LICENSE.txt')
    dependencies=['Flask','Werkzeug','Jinja2','MarkupSafe','itsdangerous','click','blinker','curl_cffi','cffi','certifi','cryptography','requests','urllib3','charset-normalizer','idna','waitress','pystray','Pillow','mini-racer','six','PyInstaller','qrcode']
    for name in dependencies:
        dist=importlib.metadata.distribution(name)
        for entry in dist.files or []:
            if any(part.lower().startswith(('license','copying','notice')) for part in entry.parts):
                source=Path(dist.locate_file(entry))
                if source.is_file():
                    target=notice_dir/name/Path(*entry.parts[-2:]);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--onedir','--windowed','--name','LingxiWorkspace',
        '--distpath',str(ROOT/'.build/dist'),'--workpath',str(ROOT/'.build/pyinstaller'),'--specpath',str(ROOT/'.build'),
        '--icon',str(assets/'app.ico'),'--noupx','--collect-all','curl_cffi','--collect-all','cryptography','--collect-all','py_mini_racer',
        '--collect-data','certifi','--hidden-import','pystray._win32',
        '--exclude-module','tkinter','--exclude-module','matplotlib','--exclude-module','numpy','--exclude-module','pytest',
        '--exclude-module','orjson']
    activity_resources = ROOT/'.build/activity-resources'
    if activity_resources.exists():
        if activity_resources.resolve().parent != (ROOT/'.build').resolve():
            raise RuntimeError('Activity staging path escaped the build directory')
        shutil.rmtree(activity_resources)
    stage_activities(ROOT, activity_resources)
    resources=[(ROOT/'static','static'),(ROOT/'templates','templates'),
        (ROOT/'app_login_public_key.json','.'),(ROOT/'mobile_rights_gateway.json','.'),(assets,'desktop_assets'),
        (ROOT/'recharge/static','recharge/static'),(ROOT/'recharge/templates','recharge/templates'),
        (ROOT/'recharge/member_benefits_runtime/dfp_runtime.js','recharge/member_benefits_runtime'),
        (ROOT/'recharge/member_benefits_runtime/wheel_runtime.js','recharge/member_benefits_runtime'),
        (ROOT/'recharge/member_benefits_runtime/assets','recharge/member_benefits_runtime/assets'),
        (activity_resources,'activities'),
        (notice_dir,'third-party-notices')]
    for source,dest in resources:command+=['--add-data',str(source)+':'+dest]
    command+=[str(ROOT/'desktop.py')]
    if '--single-file-only' not in sys.argv:subprocess.run(command,cwd=ROOT,check=True)
    package=ROOT/'.build/dist/LingxiWorkspace'
    shutil.copy2(ROOT/'DESKTOP.md',package/'使用说明.txt')
    shutil.copy2(ROOT/'THIRD_PARTY_NOTICES.md',package/'THIRD_PARTY_NOTICES.md')
    shutil.copy2(ROOT/'allow-lan.ps1',package/'allow-lan.ps1')
    release=ROOT/'release';release.mkdir(exist_ok=True)
    archive=release/f'LingxiWorkspace-{VERSION}-Windows-x64.zip'
    forbidden={'.local','.git','analysis','data','window-profile','browser-profile'}
    files=[path for path in package.rglob('*') if path.is_file()]
    for path in files:
        if forbidden.intersection(path.relative_to(package).parts) or path.suffix in {'.sqlite3','.log','.db'}:
            raise RuntimeError('Private runtime data in package: '+str(path.relative_to(package)))
        if path.name.lower() in {'chrome.exe','msedge.exe'}:raise RuntimeError('Browser must not be bundled in this release')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as output:
        for path in files:output.write(path,Path('LingxiWorkspace')/path.relative_to(package))
    command[command.index('--onedir')]='--onefile'
    subprocess.run(command,cwd=ROOT,check=True)
    executable=release/f'LingxiWorkspace-{VERSION}-Windows-x64.exe'
    shutil.copy2(ROOT/'.build/dist/LingxiWorkspace.exe',executable)
    artifacts=[executable,archive]
    android=release/f'LingxiWorkspace-{VERSION}-Android-arm64.apk'
    if android.exists():artifacts.append(android)
    (release/'SHA256SUMS.txt').write_text(''.join(sha256(path)+'  '+path.name+'\n' for path in artifacts),'utf-8')
    shutil.copy2(ROOT/'RELEASE_NOTES.md',release/f'RELEASE_NOTES-{VERSION}.md')
    print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,'files':len(files),
                      'member_benefits_runtime':'python-embedded-v8'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
