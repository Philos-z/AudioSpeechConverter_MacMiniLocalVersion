"""Create explicitly positioned Chinese-over-German ASS for the full episode.

Only subtitles are written. Source text and source timing metadata are untouched.
"""
import argparse
import json
import math
from pathlib import Path
import re

from PIL import ImageFont


FONT_PATH = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
WIDTH, HEIGHT = 1280, 720
SAFE_X, BOTTOM_Y, GAP = 72, 694, 14
MAX_WIDTH = WIDTH - 2 * SAFE_X


def _time(seconds):
    # ASS stores centiseconds; nearest rounding preserves measured boundaries.
    ticks = max(0, round(float(seconds) * 100))
    hours, ticks = divmod(ticks, 360000)
    minutes, ticks = divmod(ticks, 6000)
    whole, fraction = divmod(ticks, 100)
    return f'{hours}:{minutes:02}:{whole:02}.{fraction:02}'


def _escape(text):
    # Prevent a source backslash from forming \N/\n/\h or an override command.
    # The word-joiner is invisible and leaves the visible source text intact.
    text = text.replace('\\', '\\\u2060')
    return text.replace('{', r'\{').replace('}', r'\}')


def _width(font, text):
    return float(font.getlength(text))


def _wrap(text, font, language):
    """Insert manual line breaks without dropping or substituting characters."""
    tokens = list(text) if language == 'zh' else re.findall(r'\S+\s*|\s+', text)
    lines, current = [], ''
    for token in tokens:
        if current and _width(font, (current + token).rstrip()) > MAX_WIDTH:
            lines.append(current.rstrip())
            current = token.lstrip()
        else:
            current += token
        # Exceptionally long German words can be broken at character boundaries.
        if _width(font, current.rstrip()) > MAX_WIDTH:
            fragment = ''
            for char in current:
                if fragment and _width(font, fragment + char) > MAX_WIDTH:
                    lines.append(fragment)
                    fragment = char
                else:
                    fragment += char
            current = fragment
    if current.strip():
        lines.append(current.rstrip())
    if not lines:
        raise ValueError('Empty subtitle text')
    # Wrapping may move whitespace to a line boundary, but no text is rewritten.
    if ''.join(''.join(lines).split()) != ''.join(text.split()):
        raise ValueError('Subtitle wrapping changed source content')
    return lines


def build_ass(timeline_path, output_path):
    timeline = json.loads(Path(timeline_path).read_text(encoding='utf-8'))
    rows = timeline['segments']
    duration = float(timeline['duration_s'])
    if not FONT_PATH.is_file():
        raise FileNotFoundError(FONT_PATH)
    fonts = {'zh': ImageFont.truetype(str(FONT_PATH), 32),
             'de': ImageFont.truetype(str(FONT_PATH), 26)}
    font_name = fonts['zh'].getname()[0]
    header = f'''[Script Info]
Title: Chinese above German - private local dubbing
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Chinese,{font_name},32,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,0,0,0,0,100,100,0,0,1,3,1,2,{SAFE_X},{SAFE_X},26,1
Style: German,{font_name},26,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,0,0,0,0,100,100,0,0,1,3,1,2,{SAFE_X},{SAFE_X},26,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events, placements = [], []
    for i, row in enumerate(rows):
        start = float(row['render_start_s'])
        speech_end = float(row['render_end_s'])
        next_start = float(rows[i + 1]['render_start_s']) if i + 1 < len(rows) else duration + .03
        end = min(max(speech_end, float(row['end_s'])), next_start - .03, duration)
        if end + .001 < speech_end or end <= start:
            raise ValueError(f"Subtitle would end before speech: {row['segment_id']}")
        zh = _wrap(row['text_zh'], fonts['zh'], 'zh')
        de = _wrap(row['text_de'], fonts['de'], 'de')
        if len(de) > 3 or len(zh) > 2:
            raise ValueError(f"Unexpectedly tall subtitles: {row['segment_id']}")
        # Pillow ascender+descender is a conservative line-height bound for libass.
        de_height = len(de) * sum(fonts['de'].getmetrics())
        zh_height = len(zh) * sum(fonts['zh'].getmetrics())
        zh_bottom = BOTTOM_Y - de_height - GAP
        bounds = {'zh': [SAFE_X, zh_bottom - zh_height, WIDTH-SAFE_X, zh_bottom],
                  'de': [SAFE_X, BOTTOM_Y - de_height, WIDTH-SAFE_X, BOTTOM_Y]}
        if bounds['zh'][1] < 0 or bounds['zh'][3] + GAP > bounds['de'][1]:
            raise ValueError('Subtitle layers overlap or exceed frame')
        for language, style, lines, bottom in [('zh', 'Chinese', zh, zh_bottom), ('de', 'German', de, BOTTOM_Y)]:
            measured = [_width(fonts[language], line) for line in lines]
            if max(measured) > MAX_WIDTH + .01:
                raise ValueError('Subtitle exceeds horizontal safe area')
            content = r'\N'.join(_escape(line) for line in lines)
            positioning = f'{{\\an2\\pos({WIDTH//2},{bottom})\\q2}}'
            events.append(f'Dialogue: 0,{_time(start)},{_time(end)},{style},{row["segment_id"]},0,0,0,,{positioning}{content}')
        placements.append({'id': row['segment_id'], 'start_s': start, 'end_s': end,
                           'lines': {'zh': len(zh), 'de': len(de)}, 'bounds': bounds,
                           'max_line_width_px': {'zh': max(_width(fonts['zh'], s) for s in zh),
                                                 'de': max(_width(fonts['de'], s) for s in de)}})
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(header + '\n'.join(events) + '\n', encoding='utf-8')
    return {'output': str(target.resolve()), 'canvas': [WIDTH, HEIGHT], 'font_name': font_name,
            'font_path': str(FONT_PATH), 'font_sizes': {'zh': 32, 'de': 26},
            'safe_horizontal_margin_px': SAFE_X, 'bottom_margin_px': HEIGHT-BOTTOM_Y,
            'language_gap_px': GAP, 'source_segments': len(rows), 'events': len(events),
            'all_bounds_valid': True, 'wrap_style': 2, 'placements': placements}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('timeline_path', type=Path)
    parser.add_argument('output_path', type=Path)
    args = parser.parse_args()
    metadata = build_ass(args.timeline_path, args.output_path)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
