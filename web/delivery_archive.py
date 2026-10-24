"""Save completed, SHA-verified web deliveries in the user's 成片 folder."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata
from zoneinfo import ZoneInfo

SCHEMA = 'completed_delivery_archive_v1'
SHA = re.compile(r'^[a-f0-9]{64}$')
JID = re.compile(r'^web_\d{8}_\d{6}_[a-f0-9]{8}$')
RESERVE = 8 * 1024**3


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def safe_path(path, base):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(base):
        raise ValueError('成片文件位置不属于当前任务')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('成片归档不允许符号链接')
    return path


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix('.pending')
    if path.is_symlink() or pending.is_symlink():
        raise ValueError('归档收据不允许符号链接')
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    pending.replace(path)


def archive_delivery(root, job, artifacts):
    """Called only after the original pipeline has completed its media checks.

    Originals remain in the job for downloads and cache reuse. A real copy
    isolates the archive from future in-place edits to those working files.
    """
    root = Path(root).resolve()
    jid = job.get('id', '')
    if not JID.fullmatch(jid) or not SHA.fullmatch(str(job.get('source_sha256', ''))):
        raise ValueError('成片任务身份不完整')
    folder = safe_path(root/'jobs'/jid, root)
    entries = []
    names = set()
    for item in artifacts:
        if item.get('voice_pilot'):
            continue
        path = safe_path(Path(item['path']), folder)
        expected = item.get('sha256', '')
        if not path.is_file() or not SHA.fullmatch(str(expected)) or sha(path) != expected:
            raise ValueError('成片归档源文件校验不符')
        kind = item.get('kind', 'file')
        if kind not in {'video', 'audio', 'subtitle', 'report'}:
            raise ValueError('成片归档文件类型不合法')
        if kind == 'video' and (path.suffix.lower() != '.mp4' or not 0 < path.stat().st_size < 50_000_000):
            raise ValueError('成片视频必须严格小于50MB')
        episode = next((part for part in path.relative_to(folder).parts if re.fullmatch(r'episode_\d{3,}', part)), '')
        filename = ('中文双语成片.mp4' if kind == 'video' else
                    '中文配音与背景'+path.suffix if kind == 'audio' else
                    '中文字幕.srt' if path.name == 'zh.srt' else
                    '工程检查.json' if kind == 'report' else path.name)
        relative = Path(episode)/filename if episode else Path(filename)
        if relative.as_posix() in names:
            raise ValueError('成片归档文件名重复')
        names.add(relative.as_posix())
        entries.append({'source':str(path.relative_to(folder)), 'file':relative.as_posix(),
            'sha256':expected, 'bytes':path.stat().st_size, 'kind':kind, 'label':str(item.get('label',''))})
    if not entries or not any(item['kind'] == 'video' for item in entries):
        raise ValueError('没有已完成的视频可以归档')
    identity = {'job_id':jid, 'source_sha256':job['source_sha256'], 'artifacts':entries}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    archive_root = safe_path(root/'成片', root)
    archive_root.mkdir(exist_ok=True)
    receipt_path = safe_path(folder/'delivery_archive.json', folder)

    def existing(directory):
        safe_path(directory, archive_root)
        manifest = safe_path(directory/'归档记录.json', archive_root)
        if not manifest.is_file():
            return None
        try:
            value = json.loads(manifest.read_text())
        except (ValueError, OSError):
            return None
        if (not isinstance(value,dict) or value.get('schema') != SCHEMA
                or value.get('fingerprint') != fingerprint or value.get('directory') != str(directory)
                or any(value.get(key) != expected for key,expected in identity.items())):
            return None
        for item in entries:
            target = safe_path(directory/item['file'], archive_root)
            if not target.is_file() or target.stat().st_size != item['bytes'] or sha(target) != item['sha256']:
                return None
        return value

    if receipt_path.is_file():
        try:
            prior = json.loads(receipt_path.read_text())
        except (ValueError, OSError):
            prior = {}
        if isinstance(prior,dict) and prior.get('fingerprint') == fingerprint:
            value = existing(Path(prior['directory']))
            if value:
                return value
    title = unicodedata.normalize('NFC', str(job.get('title') or '视频译配'))
    title = re.sub(r'(?:\.webs)?\.h264$|\.(?:mp4|mov|mkv|webm)$', '', title, flags=re.I)
    title = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', title).strip(' .')[:70] or '视频译配'
    timestamp = datetime.fromisoformat(job['created_at']) if job.get('created_at') else datetime.now(timezone.utc)
    date = timestamp.astimezone(ZoneInfo('Europe/Berlin')).strftime('%Y-%m-%d')
    base = archive_root/(date+'_'+title+'_'+jid[-8:])
    target = base
    version = 0
    while target.exists():
        value = existing(target)
        if value:
            save(receipt_path, value)
            return value
        version += 1
        target = base.with_name(base.name+'_'+fingerprint[:10]+('' if version == 1 else '_'+str(version)))
    needed = sum(item['bytes'] for item in entries)
    if shutil.disk_usage(root).free < RESERVE + needed + 1024**2:
        raise RuntimeError('成片已生成，但归档空间不足；原任务视频和字幕均保留')
    staging = Path(tempfile.mkdtemp(prefix='.delivery_', dir=archive_root))
    try:
        for item in entries:
            origin = safe_path(folder/item['source'], folder)
            output = staging/item['file']
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, output)
            if sha(origin) != item['sha256'] or sha(output) != item['sha256']:
                raise RuntimeError('成片复制校验失败，原任务文件保留')
        result = {'schema':SCHEMA, 'fingerprint':fingerprint, **identity, 'directory':str(target),
            'archived_at':datetime.now(timezone.utc).isoformat(), 'originals_preserved':True,
            'human_reviewed':False, 'listening_approved':False}
        save(staging/'归档记录.json', result)
        staging.rename(target)
        save(receipt_path, result)
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging)
