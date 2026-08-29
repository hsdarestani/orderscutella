import itertools
from pathlib import Path

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


class WooClient:
    def __init__(self):
        self.base_url = (get_config('woo_url') or '').rstrip('/')
        self.consumer_key = get_config('woo_ck') or ''
        self.consumer_secret = get_config('woo_cs') or ''
        self.wp_user = get_config('wp_user') or ''
        self.wp_app_password = get_config('wp_app_password') or ''
        if not self.base_url or not self.consumer_key or not self.consumer_secret:
            raise ConfigurationError('اتصال WooCommerce تنظیم نشده است.')
        self.client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )

    def request(self, method, path, **kwargs):
        url = f'{self.base_url}/wp-json/wc/v3/{path.lstrip("/")}'
        params = dict(kwargs.pop('params', {}) or {})
        response = self.client.request(
            method,
            url,
            auth=(self.consumer_key, self.consumer_secret),
            params=params,
            **kwargs,
        )
        if response.status_code in (401, 403):
            params.update({
                'consumer_key': self.consumer_key,
                'consumer_secret': self.consumer_secret,
            })
            response = self.client.request(method, url, params=params, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f'WooCommerce HTTP {response.status_code}: {response.text[:350]}')
        return response

    def probe(self):
        response = self.request('GET', 'products', params={'per_page': 1})
        return response.headers.get('x-wp-total', '?')

    def categories(self):
        output = []
        page = 1
        while page <= 20:
            response = self.request(
                'GET', 'products/categories',
                params={'per_page': 100, 'page': page, 'hide_empty': False},
            )
            batch = response.json()
            output.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return output

    def recent_products(self, count=10):
        return self.request(
            'GET', 'products',
            params={'per_page': count, 'orderby': 'date', 'order': 'desc'},
        ).json()

    def create_product(self, data):
        return self.request('POST', 'products', json=data).json()

    def create_variation(self, product_id, data):
        return self.request('POST', f'products/{product_id}/variations', json=data).json()

    def upload_media(self, path, filename=None):
        if not self.wp_user or not self.wp_app_password:
            raise ConfigurationError('WP Username و Application Password برای آپلود عکس تنظیم نشده است.')
        path = Path(path)
        filename = filename or path.name
        mime = 'image/jpeg'
        if path.suffix.lower() == '.png':
            mime = 'image/png'
        elif path.suffix.lower() == '.webp':
            mime = 'image/webp'
        response = self.client.post(
            f'{self.base_url}/wp-json/wp/v2/media',
            auth=(self.wp_user, self.wp_app_password),
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Type': mime,
            },
            content=path.read_bytes(),
        )
        if response.status_code >= 400:
            raise RuntimeError(f'WordPress Media HTTP {response.status_code}: {response.text[:350]}')
        return response.json()

    def orders(self, statuses=('processing', 'completed', 'on-hold'), pages=20):
        output = []
        for status in statuses:
            for page in range(1, pages + 1):
                response = self.request(
                    'GET', 'orders',
                    params={'status': status, 'per_page': 100, 'page': page},
                )
                batch = response.json()
                output.extend(batch)
                if len(batch) < 100:
                    break
        unique = {}
        for order in output:
            unique[order.get('id')] = order
        return list(unique.values())

    def update_order_tracking(self, order_id, tracking_code):
        order = self.request('GET', f'orders/{order_id}').json()
        metadata = list(order.get('meta_data') or [])
        updated = False
        for item in metadata:
            if item.get('key') == '_cutella_tracking_code':
                current = str(item.get('value') or '').strip()
                if current and current != str(tracking_code):
                    raise RuntimeError(f'سفارش #{order_id} کد رهگیری متفاوت دارد و overwrite نشد.')
                item['value'] = str(tracking_code)
                updated = True
                break
        if not updated:
            metadata.append({'key': '_cutella_tracking_code', 'value': str(tracking_code)})
        return self.request(
            'PUT', f'orders/{order_id}',
            json={'meta_data': metadata},
        ).json()


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
