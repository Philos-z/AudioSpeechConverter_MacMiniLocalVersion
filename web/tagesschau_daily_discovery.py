"""Read official programme metadata and select its explicit SD 270p download.

This utility never downloads media, submits jobs, runs models or sends mail.
"""
import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

HOME_URL = 'https://www.tagesschau.de/multimedia'
BERLIN = ZoneInfo('Europe/Berlin')
CDN = 'tagesschau-progressive.ard-mcdn.de'


class NotReady(ValueError):
    def __init__(self, message, candidate=None):
        super().__init__(message)
        self.candidate = candidate


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be text')
    value = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value)
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    return datetime.fromisoformat(value)


def canonical_media(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.hostname != CDN or p.port not in (None, 443)
            or p.username or p.password or p.fragment
            or not re.fullmatch(r'/video/\d{4}/\d{4}/TV-\d{8}-\d{4}-\d{4}\.webs\.h264\.mp4', p.path)
            or p.query not in ('', 'download=true')):
        raise ValueError('SD270 must be an explicit official progressive MP4 download')
    return urlunsplit(('https', CDN, p.path, '', ''))


class Players(HTMLParser):
    def __init__(self):
        super().__init__()
        self.players = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('data-v-type') not in ('MediaPlayer', 'MediaPlayerInlinePlay'):
            return
        try:
            value = json.loads(attrs['data-v'])
        except (KeyError, ValueError, TypeError):
            return
        mc = value.get('mc') if isinstance(value, dict) else None
        if not isinstance(mc, dict) or mc.get('live'):
            return
        meta = mc.get('meta', {})
        if (meta.get('seriesTitle') != 'tagesschau'
                or not re.fullmatch(r'tagesschau \d{1,2}:\d{2} Uhr', meta.get('title', ''))):
            return
        self.players.append(mc)


def discover(html, now=None, episode_key=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Current time must include a timezone')
    parser = Players()
    parser.feed(html)
    candidates = []
    for mc in parser.players:
        meta = mc['meta']
        try:
            at = parse_time(meta['broadcastedOnDateTime'])
        except (KeyError, ValueError, TypeError):
            raise ValueError('Regular programme lacks a valid official broadcast time') from None
        if at.tzinfo is None:
            raise ValueError('Official broadcast time lacks a timezone')
        if at > now:
            continue
        clock = re.search(r'(\d{1,2}):(\d{2})', meta['title'])
        if (int(clock[1]), int(clock[2])) != (at.astimezone(BERLIN).hour, at.astimezone(BERLIN).minute):
            raise ValueError('Programme clock and official broadcast timestamp disagree')
        candidates.append((at, mc))
    if not candidates:
        raise NotReady('No current regular Tagesschau programme was found')
    if episode_key is not None:
        candidates = [(at, mc) for at, mc in candidates
            if 'tagesschau:' + at.astimezone(timezone.utc).isoformat() == episode_key]
        if not candidates:
            raise NotReady('Frozen programme is not present; do not select another episode',
                {'episode_key': episode_key})
    latest = max(at for at, _ in candidates)
    selected = []
    for at, mc in candidates:
        if at != latest:
            continue
        candidate = {'programme_title': mc['meta']['title'],
            'broadcast_at_utc': latest.astimezone(timezone.utc).isoformat(),
            'broadcast_at_berlin': latest.astimezone(BERLIN).isoformat(),
            'episode_key': 'tagesschau:' + latest.astimezone(timezone.utc).isoformat()}
        downloads = mc.get('pluginData', {}).get('download@web', {})
        if downloads.get('isEnabled') is not True:
            raise NotReady('Selected programme has not enabled its SD270 download', candidate)
        sources = [s for s in downloads.get('sources', []) if s.get('title') == 'SD 270p']
        if len(sources) != 1:
            raise NotReady('Selected programme has no unique explicit SD 270p download', candidate)
        source = sources[0]['url']
        canonical = canonical_media(source)
        streams = [media for group in mc.get('streams', []) for media in group.get('media', [])]
        matching = []
        for media in streams:
            try:
                same = canonical_media(media.get('url', '')) == canonical
            except ValueError:
                same = False
            if same:
                matching.append(media)
        if not matching or any(m.get('mimeType') != 'video/mp4'
                or m.get('maxHResolutionPx') != 480
                or ('maxVResolutionPx' in m and m['maxVResolutionPx'] != 270) for m in matching):
            raise ValueError('Official download and 480-wide progressive stream do not match')
        selected.append({'schema': 'official_tagesschau_sd270_v1', 'listing_url': HOME_URL,
            'programme_title': mc['meta']['title'], 'broadcast_at_utc': latest.astimezone(timezone.utc).isoformat(),
            'broadcast_at_berlin': latest.astimezone(BERLIN).isoformat(),
            'episode_key': 'tagesschau:' + latest.astimezone(timezone.utc).isoformat(),
            'source_url': source, 'canonical_media_url': canonical,
            'quality_label': 'SD 270p', 'expected_width': 480, 'expected_height': 270,
            'listing_html_sha256': hashlib.sha256(html.encode()).hexdigest(),
            'checked_at_utc': now.astimezone(timezone.utc).isoformat()})
    if len({v['canonical_media_url'] for v in selected}) != 1:
        raise ValueError('Latest official programme has conflicting SD270 sources')
    return selected[0]


def fetch_listing():
    with urllib.request.urlopen(HOME_URL, timeout=30) as response:
        if urlsplit(response.url).hostname != 'www.tagesschau.de':
            raise ValueError('Official listing redirected to another host')
        data = response.read(4 * 1024**2 + 1)
    if len(data) > 4 * 1024**2:
        raise ValueError('Official listing exceeds the read limit')
    return data.decode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', type=Path, help='Saved official HTML for offline verification')
    parser.add_argument('--now', help='Timezone-aware ISO time for offline verification')
    parser.add_argument('--episode-key', help='Resume only a previously frozen programme')
    parser.add_argument('--evidence-dir', type=Path)
    args = parser.parse_args()
    html = args.html.read_text() if args.html else fetch_listing()
    if args.evidence_dir:
        args.evidence_dir.mkdir(parents=True, exist_ok=False)
        (args.evidence_dir / 'listing.html').write_text(html)
    try:
        value = discover(html, parse_time(args.now) if args.now else None, args.episode_key)
    except NotReady as error:
        value = {'schema': 'tagesschau_discovery_status_v1', 'status': 'waiting_source',
            'reason': str(error), 'candidate': error.candidate,
            'listing_html_sha256': hashlib.sha256(html.encode()).hexdigest()}
        if args.evidence_dir:
            (args.evidence_dir / 'discovery_status.json').write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(value, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    if args.evidence_dir:
        (args.evidence_dir / 'discovery.json').write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
