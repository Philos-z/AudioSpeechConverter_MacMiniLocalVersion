"""Only a completed, URL-bound audio/video file may become the source."""
import hashlib
import json
import math
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from task_takeover import write_json, read_json

SCHEMA='completed_source_acquisition_v1'
EXTENSIONS={'.mp4','.mkv','.webm','.mov'}


def request_binding(url):
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname:
        raise ValueError('下载请求网址无效')
    return {'kind':'public_video_url','url_sha256':hashlib.sha256(url.encode()).hexdigest()}


def directory(root,jid,url):
    root=Path(root).resolve()
    if not isinstance(jid,str) or not jid.startswith('web_') or '/' in jid or '\\' in jid or '..' in jid:
        raise ValueError('下载任务编号无效')
    return root/'inputs'/jid/'downloads'/request_binding(url)['url_sha256']


def _safe(root,path):
    root=Path(root).resolve();path=Path(path)
    if not path.is_absolute() or not path.is_relative_to(root/'inputs'):
        raise ValueError('下载产物位置不合法')
    current=path
    while current!=root:
        if current.is_symlink():raise ValueError('下载产物不能指向符号链接')
        current=current.parent
    return path


def _sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda:stream.read(8*1024**2),b''):digest.update(data)
    return digest.hexdigest()


def _probe(path):
    tool=Path('/opt/homebrew/opt/ffmpeg-full/bin/ffprobe')
    result=subprocess.run([str(tool) if tool.is_file() else 'ffprobe','-v','error','-show_streams',
        '-show_format','-of','json',str(path)],capture_output=True,text=True,check=True,timeout=60)
    value=json.loads(result.stdout)
    kinds={s.get('codec_type') for s in value.get('streams',[])}
    try:duration=float(value.get('format',{}).get('duration',0))
    except (ValueError,TypeError):duration=0
    if not {'audio','video'}<=kinds or not math.isfinite(duration) or duration<=0:
        raise ValueError('下载结果不是完整音视频，未使用中间视频流')
    return {'duration_s':duration,'audio_streams':sum(s.get('codec_type')=='audio' for s in value['streams']),
        'video_streams':sum(s.get('codec_type')=='video' for s in value['streams'])}


def _decode_complete(path):
    tool=Path('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg')
    try:
        subprocess.run([str(tool) if tool.is_file() else 'ffmpeg','-nostdin','-v','error','-xerror',
            '-err_detect','explode','-i',str(path),'-map','0:v:0','-map','0:a:0','-f','null','-'],
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True,timeout=900)
    except (subprocess.SubprocessError,OSError):
        raise ValueError('下载源未通过完整音视频解码，已保留文件等待断点恢复') from None


def read_final_source(root,jid,url):
    root=Path(root).resolve();receipt=root/'inputs'/jid/'source_acquisition.json'
    _safe(root,receipt)
    if not receipt.is_file():return None
    value=read_json(receipt)
    if (not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('job')!=jid
            or value.get('request')!=request_binding(url)):
        raise ValueError('已保存下载与当前网址请求不一致')
    raw=value.get('source',{})
    if not isinstance(raw,dict) or not isinstance(raw.get('path'),str):
        raise ValueError('已保存下载的原片引用不完整')
    path=_safe(root,root/raw['path'])
    if (path.parent!=directory(root,jid,url) or path.stem!='source' or path.suffix.lower() not in EXTENSIONS
            or not path.is_file() or type(raw.get('bytes')) is not int
            or path.stat().st_size!=raw['bytes'] or _sha(path)!=raw.get('sha256')):
        raise ValueError('下载成片源文件身份不符，未改用其他临时流')
    if _probe(path)!=value.get('probe'):
        raise ValueError('下载成片源媒体检查不一致')
    _decode_complete(path)
    return path


def finalize(root,jid,url):
    root=Path(root).resolve();existing=read_final_source(root,jid,url)
    if existing:return existing
    folder=directory(root,jid,url);_safe(root,folder)
    candidates=[p for p in folder.glob('source.*') if p.stem=='source' and p.suffix.lower() in EXTENSIONS]
    if len(candidates)!=1:raise ValueError('下载结束但未得到唯一完整音视频，已保留中间文件')
    path=_safe(root,candidates[0]);before=path.stat();probe=_probe(path);_decode_complete(path);sha=_sha(path);after=path.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
        raise ValueError('下载源文件仍在变化，未作为原片使用')
    write_json(root/'inputs'/jid/'source_acquisition.json',{'schema':SCHEMA,'job':jid,
        'request':request_binding(url),'source':{'path':str(path.relative_to(root)),
        'bytes':after.st_size,'sha256':sha},'probe':probe,'full_decode_passed':True,
        'decode_policy':'ffmpeg_xerror_explode_all_source_av_v1','partial_streams_used':False})
    return path
