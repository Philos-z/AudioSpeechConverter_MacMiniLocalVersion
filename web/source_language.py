"""Bounded language metadata shared by the web UI and CPU Whisper runner.

This module is stdlib-only: selecting a language never loads a model, performs
a request, or allows model-produced text to become application configuration.
"""
import re


# Whisper's multilingual language codes, including Cantonese in newer models.
# Whether a particular local model recognizes a language remains model-specific.
_NAMES = (
    'en:英语 zh:中文 de:德语 es:西班牙语 ru:俄语 ko:韩语 fr:法语 ja:日语 '
    'pt:葡萄牙语 tr:土耳其语 pl:波兰语 ca:Catalan nl:荷兰语 ar:阿拉伯语 '
    'sv:瑞典语 it:意大利语 id:印度尼西亚语 hi:印地语 fi:芬兰语 vi:越南语 '
    'he:希伯来语 uk:乌克兰语 el:希腊语 ms:马来语 cs:捷克语 ro:罗马尼亚语 '
    'da:丹麦语 hu:匈牙利语 ta:泰米尔语 no:挪威语 th:泰语 ur:乌尔都语 '
    'hr:克罗地亚语 bg:保加利亚语 lt:立陶宛语 la:Latin mi:Maori ml:Malayalam '
    'cy:Welsh sk:Slovak te:Telugu fa:波斯语 lv:Latvian bn:Bengali sr:Serbian '
    'az:Azerbaijani sl:Slovenian kn:Kannada et:Estonian mk:Macedonian '
    'br:Breton eu:Basque is:Icelandic hy:Armenian ne:Nepali mn:Mongolian '
    'bs:Bosnian kk:Kazakh sq:Albanian sw:Swahili gl:Galician mr:Marathi '
    'pa:Punjabi si:Sinhala km:Khmer sn:Shona yo:Yoruba so:Somali af:Afrikaans '
    'oc:Occitan ka:Georgian be:Belarusian tg:Tajik sd:Sindhi gu:Gujarati '
    'am:Amharic yi:Yiddish lo:Lao uz:Uzbek fo:Faroese ht:Haitian '
    'ps:Pashto tk:Turkmen nn:Nynorsk mt:Maltese sa:Sanskrit lb:Luxembourgish '
    'my:缅甸语 bo:Tibetan tl:Tagalog mg:Malagasy as:Assamese tt:Tatar '
    'haw:Hawaiian ln:Lingala ha:Hausa ba:Bashkir jw:Javanese su:Sundanese yue:粤语'
)
SUPPORTED_LANGUAGES = dict(item.split(':', 1) for item in _NAMES.split())
_ALIASES = {
    '自动': 'auto', '自动识别': 'auto', 'detect': 'auto',
    'german': 'de', 'deutsch': 'de', 'english': 'en', 'french': 'fr',
    'spanish': 'es', 'japanese': 'ja', 'chinese': 'zh', 'korean': 'ko',
    'portuguese': 'pt', 'italian': 'it', 'russian': 'ru', 'arabic': 'ar',
    'jv': 'jw', 'iw': 'he',
    **{label.lower(): code for code, label in SUPPORTED_LANGUAGES.items()},
}


def normalize_language(value, allow_auto=True):
    """Return one supported code; reject arbitrary text/configuration outright."""
    if value is None:
        value = 'auto'
    if not isinstance(value, str):
        raise ValueError('源语言必须是语言代码或自动识别')
    code = value.strip().lower()
    code = _ALIASES.get(code, code)
    # Accept ordinary BCP-47 hints such as en-US/pt-BR, never arbitrary suffixes.
    if re.fullmatch(r'[a-z]{2,3}[-_](?:[a-z]{2}|[a-z]{4})(?:[-_][a-z]{2})?', code):
        code = code.split('-')[0].split('_')[0]
    if code == 'auto' and allow_auto:
        return code
    if code not in SUPPORTED_LANGUAGES:
        raise ValueError('不支持的源语言代码；请选择自动识别或受支持的语言')
    return code


def language_label(value):
    code = normalize_language(value)
    return '自动识别' if code == 'auto' else SUPPORTED_LANGUAGES[code]


def source_language_metadata(value):
    code = normalize_language(value)
    return {'source_language': code, 'source_language_label': language_label(code)}
