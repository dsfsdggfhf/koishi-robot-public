"""Read one bounded Bilibili DM image without forwarding its URL or cookies."""
import asyncio
import base64
from io import BytesIO
import json
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps

MAX_BYTES = 4 * 1024 * 1024


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048 or '\\' in value or any(c.isspace() for c in value):
        return ''
    if value.startswith('//'):
        value = 'https:' + value
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.port is not None:
            return ''
        if not host.endswith(('.hdslb.com', '.biliimg.com')) or not parsed.path.startswith('/'):
            return ''
        return parsed._replace(scheme='https', fragment='').geturl()
    except ValueError:
        return ''


def message_url(message):
    if not message or str(message.get('msg_type')) != '2':
        return ''
    content = message.get('content')
    try:
        data = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    for key in ('url', 'img_url', 'img_src', 'image_url', 'original', 'original_url'):
        url = safe_url(data.get(key))
        if url:
            return url
    for key in ('urls', 'images', 'pictures', 'imgs'):
        items = data.get(key)
        if isinstance(items, list):
            for item in items[:10]:
                url = safe_url(item.get('url') or item.get('img_url') or item.get('img_src') if isinstance(item, dict) else item)
                if url:
                    return url
    return ''


def _jpeg_data_url(data):
    with Image.open(BytesIO(data), formats=['JPEG', 'PNG', 'WEBP', 'GIF']) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError('image_dimensions_too_large')
        frame = ImageOps.exif_transpose(image)
        frame.thumbnail((768, 768))
        out = BytesIO()
        frame.convert('RGB').save(out, format='JPEG', quality=82)
    return 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode('ascii')


async def image_data_url(url):
    url = safe_url(url)
    if not url:
        raise ValueError('untrusted_dm_image_url')
    async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
        async with client.stream('GET', url) as response:
            response.raise_for_status()
            if not response.headers.get('content-type', '').lower().startswith('image/'):
                raise ValueError('not_an_image')
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError('dm_image_too_large')
    return await asyncio.to_thread(_jpeg_data_url, data)
