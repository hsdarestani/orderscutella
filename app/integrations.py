import base64
import hashlib
import hmac
import http.client
import itertools
import json
import os
import secrets
import ssl
import time
import zlib
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from .db import get_config

SHOPINO_BASE = 'https://api.shopino.app/api/v1/shop-panel'
SHOPINO_HEADERS = {
    'accept': 'application/json',
    'content-type': 'application/json',
    'origin': 'https://panel.shopino.app',
    'referer': 'https://panel.shopino.app/',
    'user-agent': 'Mozilla/5.0 (OrdersCutellaBot; Shopino panel integration)',
}

BRIDGE_HEADERS = {
    'accept': 'application/json,text/plain,*/*',
    'cache-control': 'no-cache',
    'pragma': 'no-cache',
    'user-agent': 'Mozilla/5.0 (OrdersCutellaBot; signed bridge)',
}
MEDIA_CHUNK_SIZE = 3072


class ConfigurationError(RuntimeError):
    pass


class ShopinoAuthError(RuntimeError):
    pass


class ShopinoClient:
    def __init__(self):
        self.shop_id = get_config('shopino_shop_id')
        self.session_id = get_config('shopino_session')
        if not self.shop_id:
            raise ConfigurationError('Shopino Shop ID تنظیم نشده است.')
        if not self.session_id:
            raise ConfigurationError('سشن Shopino تنظیم نشده است.')
        headers = dict(SHOPINO_HEADERS)
        headers['cookie'] = f'sessionid={self.session_id}'
        self.client = httpx.Client(
            timeout=httpx.Timeout(35.0, connect=10.0),
            follow_redirects=True,
            headers=headers,
            trust_env=False,
        )

    def _check(self, response):
        if response.status_code in (401, 403):
            raise ShopinoAuthError('سشن Shopino منقضی یا نامعتبر است.')
        if response.status_code >= 400:
            raise RuntimeError(f'Shopino HTTP {response.status_code}: {response.text[:300]}')
        return response

    def probe(self):
        response = self.client.get(
            f'{SHOPINO_BASE}/shops/{self.shop_id}/order-shippings/',
            params={'page': 1, 'page_size': 1},
        )
        data = self._check(response).json()
        return data.get('count', '?')

    def orders(self, limit=5000):
        url = f'{SHOPINO_BASE}/shops/{self.shop_id}/order-shippings/'
        params = {'page': 1, 'page_size': 100}
        output = []
        seen = set()
        while url and len(output) < limit:
            response = self.client.get(url, params=params)
            data = self._check(response).json()
            params = None
            for item in data.get('results', []):
                item_id = item.get('id')
                if item_id in seen:
                    continue
                seen.add(item_id)
                output.append(item)
            url = data.get('next')
        return output

    def set_tracking(self, shipping_id, tracking_code, shipping_type='post'):
        response = self.client.patch(
            f'{SHOPINO_BASE}/shops/{self.shop_id}/order-shippings/{shipping_id}/',
            json={'tracking_code': str(tracking_code), 'type': shipping_type or 'post'},
        )
        self._check(response)
        return True


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


class _BridgeResponse:
    def __init__(self, status_code, headers, body):
        self.status_code = int(status_code)
        self.headers = headers
        self._body = body

    @property
    def text(self):
        return self._body.decode('utf-8', errors='replace')

    def json(self):
        return json.loads(self.text)


class WooClient:
    """WooCommerce client using the Cutella signed WordPress bridge.

    Cloudflare relay is preferred. Direct WordPress access exists only as a
    compatibility fallback and raw TLS errors are never exposed to Telegram.
    """

    def __init__(self):
        self.base_url = (get_config('woo_url') or '').rstrip('/')
        self.bridge_token = get_config('woo_bridge_token') or ''
        self.relay_url = (
            get_config('woo_relay_url')
            or os.getenv('BRIDGE_RELAY_URL')
            or ''
        ).rstrip('/')
        if not self.base_url:
            raise ConfigurationError('آدرس سایت Cutella تنظیم نشده است.')
        if not self.bridge_token:
            raise ConfigurationError('Bridge Token تنظیم نشده است.')

    def _signed_params(self, op, payload=None):
        raw = json.dumps(payload or {}, ensure_ascii=False, separators=(',', ':')).encode()
        packed = _b64url(zlib.compress(raw, 9)) if payload else ''
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = f'v2|{ts}|{nonce}|{op}|{packed}'
        sig = hmac.new(self.bridge_token.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {'cbb': '2', 't': ts, 'n': nonce, 'o': op, 'd': packed, 's': sig}

    def _endpoints(self):
        if self.relay_url:
            return [('cloudflare-relay', f'{self.relay_url}/')]
        return [
            ('cutella-home', f'{self.base_url}/'),
            ('cutella-index', f'{self.base_url}/index.php'),
        ]

    def _stdlib_get(self, endpoint, params, timeout=12.0):
        target = urlsplit(endpoint)
        if target.scheme != 'https' or not target.hostname:
            raise RuntimeError('Bridge URL باید HTTPS معتبر باشد.')
        path = target.path or '/'
        query = urlencode(params)
        if target.query:
            query = f'{target.query}&{query}'
        if query:
            path = f'{path}?{query}'
        connection = http.client.HTTPSConnection(
            target.hostname,
            target.port or 443,
            timeout=max(0.1, float(timeout)),
            context=ssl.create_default_context(),
        )
        try:
            connection.request('GET', path, headers=BRIDGE_HEADERS)
            response = connection.getresponse()
            return _BridgeResponse(response.status, response.headers, response.read())
        finally:
            connection.close()

    def _decode(self, response):
        try:
            body = response.json()
        except Exception:
            raise RuntimeError('Bridge پاسخ JSON معتبر نداد.')
        if response.status_code >= 400 or not isinstance(body, dict) or not body.get('success'):
            data = body.get('data') if isinstance(body, dict) else None
            if isinstance(data, dict):
                detail = data.get('message') or 'Bridge request failed.'
            else:
                detail = str(data or 'Bridge request failed.')
            raise RuntimeError(f'Bridge: {detail}')
        data = body.get('data')
        return data if isinstance(data, dict) else {}

    def _signed_get(self, op, payload=None):
        errors = []
        deadline = time.monotonic() + 15.0
        for label, endpoint in self._endpoints():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            params = self._signed_params(op, payload)
            try:
                response = self._stdlib_get(endpoint, params, min(12.0, remaining))
                return self._decode(response)
            except RuntimeError as exc:
                # Application-level bridge errors are useful and safe to show.
                if str(exc).startswith('Bridge:'):
                    raise
                errors.append(f'{label}:{type(exc).__name__}')
            except Exception as exc:
                errors.append(f'{label}:{type(exc).__name__}')
        if self.relay_url:
            raise RuntimeError('اتصال امن Cloudflare به Cutella موقتاً در دسترس نیست؛ چند لحظه بعد دوباره امتحان کن.')
        raise RuntimeError('مسیر مستقیم Cutella پایدار نیست. Cloudflare Relay را در تنظیمات اتصال فعال کن.')

    def probe(self):
        return self._signed_get('ping').get('product_count', '?')

    def categories(self):
        return self._signed_get('categories').get('categories', [])

    def recent_products(self, count=10):
        result = self._signed_get('recent_products', {'count': int(count)})
        return result.get('products', [])

    def create_product(self, data):
        return self._signed_get('create_product', data)

    def create_variation(self, product_id, data):
        return self._signed_get('create_variation', {
            'product_id': int(product_id),
            'variation': data,
        })

    def upload_media(self, path, filename=None):
        path = Path(path)
        data = path.read_bytes()
        if not data:
            raise RuntimeError('فایل تصویر خالی است.')
        upload_id = secrets.token_hex(16)
        digest = hashlib.sha256(data).hexdigest()
        safe_name = Path(filename or path.name).name or 'cutella-product.jpg'
        begin = self._signed_get('media_begin', {
            'upload_id': upload_id,
            'filename': safe_name,
            'size': len(data),
            'sha256': digest,
        })
        if begin.get('already_finished') and isinstance(begin.get('result'), dict):
            return begin['result']
        for offset in range(0, len(data), MEDIA_CHUNK_SIZE):
            chunk = data[offset:offset + MEDIA_CHUNK_SIZE]
            self._signed_get('media_chunk', {
                'upload_id': upload_id,
                'offset': offset,
                'data': _b64url(chunk),
            })
        return self._signed_get('media_finish', {'upload_id': upload_id})

    def orders(self, statuses=('processing', 'completed', 'on-hold'), pages=20):
        result = self._signed_get('orders', {
            'statuses': list(statuses),
            'limit': min(5000, max(1, int(pages) * 100)),
        })
        return result.get('orders', [])

    def update_order_tracking(self, order_id, tracking_code):
        return self._signed_get('update_order_tracking', {
            'order_id': int(order_id),
            'tracking_code': str(tracking_code),
        })


def shopino_name(item):
    order = item.get('order') or {}
    return f"{order.get('first_name', '')} {order.get('last_name', '')}".strip()


def shopino_phone(item):
    order = item.get('order') or {}
    return str(order.get('phone') or order.get('mobile') or '').strip()


def woo_name(order):
    shipping = order.get('shipping') or {}
    billing = order.get('billing') or {}
    first = shipping.get('first_name') or billing.get('first_name') or ''
    last = shipping.get('last_name') or billing.get('last_name') or ''
    return f'{first} {last}'.strip()


def woo_phone(order):
    billing = order.get('billing') or {}
    return str(billing.get('phone') or '').strip()


def build_variations(attribute_map, regular_price, sale_price=None):
    names = list(attribute_map.keys())
    values = [attribute_map[name] for name in names]
    output = []
    for combo in itertools.product(*values):
        data = {
            'regular_price': str(regular_price),
            'attributes': [
                {'name': name, 'option': value}
                for name, value in zip(names, combo)
            ],
        }
        if sale_price:
            data['sale_price'] = str(sale_price)
        output.append(data)
    return output
