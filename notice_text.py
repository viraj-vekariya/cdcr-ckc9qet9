"""Lossless notice layout helpers: keep source URLs and wording intact."""
import re

URL = re.compile(r'https?://[^\s<>"\']+')

def normalize(text):
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n').replace('\xa0', ' ')
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(line.rstrip() for line in text.splitlines())).strip()

def detail_body(text):
    text = normalize(text)
    # ShowContent's first three fields precede the actual notice body.
    return re.sub(r'\AType\s*:[^\n]*\n+Subject\s*:[^\n]*\n+Company\s*:[^\n]*\n+', '', text, count=1).strip()

def valid_layout(source, candidate):
    # Gemini is permitted to change whitespace only; links are indivisible.
    return (re.sub(r'\s+', '', source) == re.sub(r'\s+', '', candidate)
            and URL.findall(source) == URL.findall(candidate))
