"""Offline discovery counterexamples. No network, media, model, job or mail use."""
import copy
import contextlib
from datetime import datetime
import html
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import tagesschau_daily_discovery as discovery


def programme(stamp='2026-11-02T19:00:00+00:00',title='tagesschau 20:00 Uhr',asset='0001'):
    url='https://%s/video/2099/0101/TV-20990101-0000-%s.webs.h264.mp4'%(discovery.CDN,asset)
    return {'meta':{'seriesTitle':'tagesschau','title':title,'broadcastedOnDateTime':stamp},'live':False,
        'pluginData':{'download@web':{'isEnabled':True,'sources':[{'title':'SD 270p','url':url+'?download=true'}]}},
        'streams':[{'media':[{'url':url,'mimeType':'video/mp4','maxHResolutionPx':480,'maxVResolutionPx':270}]}]}


def listing(*players):
    return ''.join('<div data-v-type="MediaPlayer" data-v="%s"></div>'%
        html.escape(json.dumps({'mc':player}),quote=True) for player in players)


class DiscoveryIndependent(unittest.TestCase):
    NOW=datetime.fromisoformat('2026-11-03T00:30:00+00:00')
    def choose(self,*players,now=None):return discovery.discover(listing(*players),now or self.NOW)

    def test_latest_broadcast_wins_regardless_of_dom_order_or_20_uhr_section(self):
        old=programme();latest=programme('2026-11-02T23:10:00+00:00','tagesschau 00:10 Uhr','0002')
        for items in [(old,latest),(latest,old)]:
            result=self.choose(*items)
            self.assertEqual(result['programme_title'],'tagesschau 00:10 Uhr')
            self.assertEqual(result['broadcast_at_berlin'],'2026-11-03T00:10:00+01:00')
            self.assertIn('0002.webs.h264.mp4',result['source_url'])

    def test_excludes_short_simple_sign_language_tagesthemen_clips_and_live(self):
        valid=programme()
        for title,series in [('tagesschau in 100 Sekunden','tagesschau'),('tagesschau in Einfacher Sprache','tagesschau'),
            ('tagesschau 20:00 Uhr mit Gebärdensprache','tagesschau'),('tagesthemen 22:15 Uhr','tagesthemen'),
            ('tagesschau 20:00 Uhr - Beitrag','tagesschau'),('tagesschau 20:00 Uhr','tagesschau in 100 Sekunden')]:
            excluded=programme('2026-11-02T22:00:00+00:00',title,'0002');excluded['meta']['seriesTitle']=series
            with self.subTest(title=title,series=series):self.assertEqual(self.choose(excluded,valid)['programme_title'],valid['meta']['title'])
        live=programme('2026-11-02T23:10:00+00:00','tagesschau 00:10 Uhr','0002');live['live']=True
        self.assertEqual(self.choose(live,valid)['programme_title'],valid['meta']['title'])

    def test_latest_missing_disabled_duplicate_or_relabelled_sd_never_falls_back(self):
        for mutation in ('missing','disabled','duplicate','sd360'):
            latest=programme('2026-11-02T23:10:00+00:00','tagesschau 00:10 Uhr','0002')
            download=latest['pluginData']['download@web']
            if mutation=='missing':download['sources']=[]
            elif mutation=='disabled':download['isEnabled']=False
            elif mutation=='duplicate':download['sources']*=2
            else:download['sources'][0]['title']='SD 360p'
            with self.subTest(mutation=mutation),self.assertRaises(discovery.NotReady):self.choose(programme(),latest)
        missing=programme('2026-11-02T23:10:00+00:00','tagesschau 00:10 Uhr','0002')
        missing['pluginData']['download@web']['sources']=[]
        with self.assertRaises(discovery.NotReady) as captured:self.choose(programme(),missing)
        frozen=captured.exception.candidate
        self.assertEqual(frozen['programme_title'],'tagesschau 00:10 Uhr')
        key=frozen['episode_key']
        newer=programme('2026-11-03T00:20:00+00:00','tagesschau 01:20 Uhr','0003')
        with self.assertRaises(discovery.NotReady):
            discovery.discover(listing(newer,missing),self.NOW,episode_key=key)
        ready=programme('2026-11-02T23:10:00+00:00','tagesschau 00:10 Uhr','0002')
        self.assertEqual(discovery.discover(listing(newer,ready),self.NOW,episode_key=key)['episode_key'],key)
        with self.assertRaises(discovery.NotReady) as absent:
            discovery.discover(listing(newer),self.NOW,episode_key=key)
        self.assertEqual(absent.exception.candidate['episode_key'],key)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'saved.html';source.write_text(listing(newer,missing));evidence=root/'observation'
            argv=['discovery','--html',str(source),'--now',self.NOW.isoformat(),'--episode-key',key,'--evidence-dir',str(evidence)]
            with patch.object(sys,'argv',argv),patch.object(discovery,'fetch_listing',side_effect=AssertionError('offline only')),\
                 contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SystemExit) as exit_code:
                discovery.main()
            self.assertEqual(exit_code.exception.code,2)
            self.assertEqual((evidence/'listing.html').read_text(),source.read_text())
            status=json.loads((evidence/'discovery_status.json').read_text())
            self.assertEqual(status['status'],'waiting_source');self.assertEqual(status['candidate'],frozen)
            with patch.object(sys,'argv',argv),self.assertRaises(FileExistsError):discovery.main()

    def test_same_time_conflicting_sources_are_rejected_identical_cards_are_allowed(self):
        first=programme();second=programme(asset='0002')
        with self.assertRaises(ValueError):self.choose(first,second)
        self.assertEqual(self.choose(first,copy.deepcopy(first))['canonical_media_url'],
            self.choose(first)['canonical_media_url'])

    def test_future_naive_and_disagreeing_clock_are_not_reinterpreted(self):
        future=programme('2026-11-03T19:00:00+00:00','tagesschau 20:00 Uhr','0002')
        self.assertEqual(self.choose(future,programme())['programme_title'],'tagesschau 20:00 Uhr')
        with self.assertRaises(discovery.NotReady):self.choose(future)
        with self.assertRaises(ValueError):self.choose(programme(),now=datetime(2026,11,3))
        with self.assertRaises(ValueError):self.choose(programme('2026-11-02T19:00:00','tagesschau 20:00 Uhr'))
        with self.assertRaises(ValueError):self.choose(programme('2026-11-02T19:00:00+00:00','tagesschau 19:00 Uhr'))

    def test_berlin_dst_fold_and_spring_offset_keep_utc_episode_order(self):
        earlier=programme('2026-10-25T00:30:00+00:00','tagesschau 02:30 Uhr','0001')
        later=programme('2026-10-25T01:30:00+00:00','tagesschau 02:30 Uhr','0002')
        chosen=self.choose(later,earlier,now=datetime.fromisoformat('2026-10-25T02:00:00+00:00'))
        self.assertEqual(chosen['broadcast_at_berlin'],'2026-10-25T02:30:00+01:00')
        self.assertNotEqual(self.choose(earlier,now=self.NOW)['episode_key'],chosen['episode_key'])
        spring=programme('2026-03-29T01:30:00+00:00','tagesschau 03:30 Uhr')
        self.assertEqual(self.choose(spring)['broadcast_at_berlin'],'2026-03-29T03:30:00+02:00')
        self.assertEqual(discovery.parse_time('2026-11-02T23:10:00+0000'),discovery.parse_time('2026-11-02T23:10:00Z'))

    def test_download_host_path_query_label_and_stream_identity_are_strict(self):
        original=programme();url=original['pluginData']['download@web']['sources'][0]['url']
        invalid=[url.replace(discovery.CDN,'evil.example'),url.replace('https:','http:'),
            url.replace('.webs.','.webxl.'),url+'#fragment',url.replace('?download=true','?download=true&other=1'),
            url.replace(discovery.CDN,'person@'+discovery.CDN)]
        for bad_url in invalid:
            changed=copy.deepcopy(original);changed['pluginData']['download@web']['sources'][0]['url']=bad_url
            with self.subTest(download=bad_url),self.assertRaises(ValueError):self.choose(changed)
        for mutation in ('other_host','http','other_path','wrong_mime','width','height','conflicting_duplicate'):
            changed=copy.deepcopy(original);media=changed['streams'][0]['media'][0]
            if mutation=='other_host':media['url']=media['url'].replace(discovery.CDN,'evil.example')
            elif mutation=='http':media['url']=media['url'].replace('https:','http:')
            elif mutation=='other_path':media['url']=media['url'].replace('0001.webs','0002.webs')
            elif mutation=='wrong_mime':media['mimeType']='audio/mpeg'
            elif mutation=='width':media['maxHResolutionPx']=1280
            elif mutation=='height':media['maxVResolutionPx']=720
            else:
                extra=copy.deepcopy(media);extra['maxHResolutionPx']=1280
                changed['streams'][0]['media'].append(extra)
            with self.subTest(stream=mutation),self.assertRaises(ValueError):self.choose(changed)



if __name__=='__main__':unittest.main()
