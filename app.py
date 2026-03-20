from __future__ import annotations

import html
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', '8000'))
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36'
CACHE_TTL_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 20
SHIPPING_COST = 7900
PURCHASE_DISCOUNT = 0.10
DUCKDUCKGO_HTML = 'https://html.duckduckgo.com/html/'

CACHE: dict[str, tuple[float, Any]] = {}


@dataclass
class Offer:
    source: str
    title: str
    url: str
    price: int | None
    isbn: str | None
    condition: str | None
    status: str | None


@dataclass
class SearchResponse:
    query: str
    sale_offer: dict[str, Any] | None
    purchase_price: int | None
    utility: int | None
    shipping_price: int | None
    market_offers: list[dict[str, Any]]
    market_reference_price: int | None
    suggested_sale_price: int | None
    suggested_utility: int | None
    warnings: list[str]


def normalize_whitespace(value: str) -> str:
    return re.sub(r'\s+', ' ', html.unescape(value or '')).strip()


def format_cop(value: int | None) -> str:
    if value is None:
        return 'No disponible'
    return f"$ {value:,.0f}".replace(',', '.')


def round_down_hundred(value: float) -> int:
    return int(math.floor(value / 100.0) * 100)


def get_cached(key: str) -> Any | None:
    cached = CACHE.get(key)
    if not cached:
        return None
    expires_at, payload = cached
    if expires_at < time.time():
        CACHE.pop(key, None)
        return None
    return payload


def set_cached(key: str, payload: Any) -> Any:
    CACHE[key] = (time.time() + CACHE_TTL_SECONDS, payload)
    return payload


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Language': 'es-CO,es;q=0.9,en;q=0.8'})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        charset = response.headers.get_content_charset() or 'utf-8'
        return response.read().decode(charset, errors='replace')


def search_duckduckgo(query: str, domain: str | None = None, limit: int = 8) -> list[str]:
    composed_query = query if not domain else f'site:{domain} {query}'
    cache_key = f'ddg::{composed_query}::{limit}'
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    params = urllib.parse.urlencode({'q': composed_query})
    html_text = fetch_text(f'{DUCKDUCKGO_HTML}?{params}')
    urls: list[str] = []
    for match in re.finditer(r'nofollow" class="[^\"]*result__a[^\"]*" href="([^"]+)"', html_text):
        href = html.unescape(match.group(1))
        parsed = urllib.parse.urlparse(href)
        if 'duckduckgo.com' in parsed.netloc:
            qs = urllib.parse.parse_qs(parsed.query)
            href = qs.get('uddg', [href])[0]
        if domain and domain not in href:
            continue
        if href not in urls:
            urls.append(href)
        if len(urls) >= limit:
            break
    return set_cached(cache_key, urls)


def extract_json_ld_payloads(html_text: str) -> list[Any]:
    payloads: list[Any] = []
    for match in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html_text, re.S | re.I):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payloads.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return payloads


def flatten_json_ld(node: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(node, dict):
        items.append(node)
        if '@graph' in node and isinstance(node['@graph'], list):
            for child in node['@graph']:
                items.extend(flatten_json_ld(child))
    elif isinstance(node, list):
        for child in node:
            items.extend(flatten_json_ld(child))
    return items


def parse_price(raw: str | int | float | None) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    digits = re.sub(r'[^\d]', '', raw)
    return int(digits) if digits else None


def extract_isbn(text: str) -> str | None:
    match = re.search(r'ISBN(?:-13)?[^\d]*(97[89][-\s]?\d[-\d\s]{8,16}|\d[-\d\s]{8,16})', text, re.I)
    if not match:
        return None
    digits = re.sub(r'[^\dXx]', '', match.group(1))
    return digits or None


def parse_offer_from_product_page(source: str, url: str, html_text: str) -> Offer:
    text = normalize_whitespace(re.sub(r'<[^>]+>', ' ', html_text))
    condition = None
    used_idx = text.lower().find('usado')
    new_idx = text.lower().find('nuevo')
    if new_idx != -1 and (used_idx == -1 or new_idx < used_idx):
        condition = 'Nuevo'
    elif used_idx != -1:
        condition = 'Usado'

    title = None
    price = None
    status = None
    isbn = extract_isbn(text)

    for payload in extract_json_ld_payloads(html_text):
        for item in flatten_json_ld(payload):
            item_type = item.get('@type')
            if item_type in ('Product', ['Product']):
                title = title or normalize_whitespace(str(item.get('name') or ''))
                sku = item.get('sku') or item.get('gtin13') or item.get('gtin')
                if sku and not isbn:
                    isbn = re.sub(r'[^\dXx]', '', str(sku))
                offers = item.get('offers')
                if isinstance(offers, dict):
                    price = price or parse_price(offers.get('price'))
                    status = status or normalize_whitespace(str(offers.get('availability') or '').split('/')[-1])
            if item_type == 'Offer':
                price = price or parse_price(item.get('price'))
                status = status or normalize_whitespace(str(item.get('availability') or '').split('/')[-1])

    meta_title = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html_text, re.I)
    if meta_title and not title:
        title = normalize_whitespace(meta_title.group(1))

    meta_price = re.search(r'<meta[^>]+(?:property|name)="(?:product:price:amount|twitter:data1)"[^>]+content="([^"]+)"', html_text, re.I)
    if meta_price and price is None:
        price = parse_price(meta_price.group(1))

    visible_price = re.search(r'\$\s*([\d\.,]+)', text)
    if visible_price and price is None:
        price = parse_price(visible_price.group(1))

    return Offer(
        source=source,
        title=title or 'Sin título reconocido',
        url=url,
        price=price,
        isbn=isbn,
        condition=condition,
        status=status,
    )


def search_buscalibre(query: str) -> tuple[Offer | None, list[str]]:
    warnings: list[str] = []
    urls = search_duckduckgo(f'"{query}" libro', domain='buscalibre.com.co', limit=10)
    product_urls = [url for url in urls if '/libro' in urllib.parse.urlparse(url).path]
    offers: list[Offer] = []

    for url in product_urls:
        try:
            offer = parse_offer_from_product_page('Buscalibre', url, fetch_text(url))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'No se pudo abrir {url}: {exc}')
            continue
        if query.lower() not in offer.title.lower() and query.lower() not in normalize_whitespace(url.replace('-', ' ')).lower():
            continue
        if offer.condition == 'Usado':
            continue
        if offer.price is None:
            continue
        offers.append(offer)

    if not offers:
        warnings.append('No se encontraron coincidencias nuevas válidas en Buscalibre usando el buscador independiente.')
        return None, warnings

    offers.sort(key=lambda item: item.price or 10**12)
    return offers[0], warnings


def search_market_by_isbn(isbn: str) -> tuple[list[Offer], list[str]]:
    warnings: list[str] = []
    offers: list[Offer] = []
    domains = [('Tornamesa', 'tornamesa.co'), ('Casa del Libro', 'casadellibro.com.co')]
    for source_name, domain in domains:
        try:
            urls = search_duckduckgo(isbn, domain=domain, limit=5)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'No se pudo consultar {source_name}: {exc}')
            continue
        for url in urls:
            try:
                offer = parse_offer_from_product_page(source_name, url, fetch_text(url))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f'No se pudo abrir {url}: {exc}')
                continue
            if offer.price is None:
                continue
            normalized_isbn = re.sub(r'[^\dXx]', '', offer.isbn or '')
            if normalized_isbn and normalized_isbn != isbn:
                continue
            offers.append(offer)
            break
    return offers, warnings


def build_response(query: str) -> SearchResponse:
    try:
        sale_offer, sale_warnings = search_buscalibre(query)
        warnings = list(sale_warnings)
    except Exception as exc:  # noqa: BLE001
        return SearchResponse(
            query=query,
            sale_offer=None,
            purchase_price=None,
            utility=None,
            shipping_price=None,
            market_offers=[],
            market_reference_price=None,
            suggested_sale_price=None,
            suggested_utility=None,
            warnings=[f'No fue posible consultar fuentes externas: {exc}'],
        )
    if sale_offer is None or sale_offer.price is None:
        return SearchResponse(
            query=query,
            sale_offer=None,
            purchase_price=None,
            utility=None,
            shipping_price=None,
            market_offers=[],
            market_reference_price=None,
            suggested_sale_price=None,
            suggested_utility=None,
            warnings=warnings,
        )

    purchase_price = round_down_hundred(sale_offer.price * (1 - PURCHASE_DISCOUNT))
    utility = sale_offer.price - purchase_price
    shipping_price = sale_offer.price + SHIPPING_COST

    market_offers: list[Offer] = []
    market_reference_price = None
    suggested_sale_price = None
    suggested_utility = None

    if sale_offer.isbn:
        found_market_offers, market_warnings = search_market_by_isbn(sale_offer.isbn)
        warnings.extend(market_warnings)
        market_offers = found_market_offers
        market_prices = [offer.price for offer in market_offers if offer.price is not None]
        if market_prices:
            market_reference_price = min(market_prices)
            if market_reference_price > sale_offer.price:
                midpoint = round_down_hundred((sale_offer.price + market_reference_price) / 2)
                suggested_sale_price = min(market_reference_price, max(sale_offer.price, midpoint))
                suggested_utility = suggested_sale_price - purchase_price
    else:
        warnings.append('El libro encontrado en Buscalibre no expuso un ISBN reconocible, así que no fue posible consultar Tornamesa y Casa del Libro.')

    return SearchResponse(
        query=query,
        sale_offer=asdict(sale_offer),
        purchase_price=purchase_price,
        utility=utility,
        shipping_price=shipping_price,
        market_offers=[asdict(offer) for offer in market_offers],
        market_reference_price=market_reference_price,
        suggested_sale_price=suggested_sale_price,
        suggested_utility=suggested_utility,
        warnings=warnings,
    )


INDEX_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Comparador de libros</title>
  <style>
    :root { color-scheme: light; }
    body { font-family: Arial, sans-serif; margin: 0; background: #f4f7fb; color: #172033; }
    main { max-width: 1100px; margin: 0 auto; padding: 32px 20px 56px; }
    h1 { margin-bottom: 8px; }
    .lead { color: #4e5d78; line-height: 1.5; max-width: 850px; }
    form { display: flex; gap: 12px; flex-wrap: wrap; margin: 24px 0; }
    input { flex: 1 1 320px; border: 1px solid #c8d1e2; border-radius: 12px; padding: 14px 16px; font-size: 16px; }
    button { border: 0; border-radius: 12px; background: #265df2; color: white; padding: 14px 22px; font-size: 16px; cursor: pointer; }
    button:disabled { opacity: .7; cursor: wait; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; margin: 20px 0 28px; }
    .card { background: white; border-radius: 16px; padding: 18px; box-shadow: 0 10px 30px rgba(35,57,99,.08); }
    .card h3 { margin: 0 0 8px; font-size: 14px; text-transform: uppercase; letter-spacing: .04em; color: #60708f; }
    .value { font-size: 28px; font-weight: 700; }
    .subtle { color: #60708f; font-size: 14px; }
    .section { margin-top: 24px; }
    .offer { background: white; border-radius: 16px; padding: 18px; margin-bottom: 14px; box-shadow: 0 10px 30px rgba(35,57,99,.08); }
    .pill { display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px; background: #e8eefc; color: #265df2; margin-right: 8px; }
    .warning { background: #fff8e8; color: #7a5b00; border-left: 4px solid #d8a700; padding: 12px 14px; border-radius: 10px; margin: 12px 0; }
    .hidden { display: none; }
    a { color: #265df2; }
  </style>
</head>
<body>
  <main>
    <h1>Buscador independiente de precios de libros</h1>
    <p class="lead">Busca el mejor precio nuevo en Buscalibre sin depender del buscador interno. Luego calcula <strong>precio de compra</strong>, <strong>utilidad</strong>, <strong>precio con envío</strong> y, usando el ISBN del mejor resultado, compara Tornamesa y Casa del Libro para proponer un precio de venta más rentable sin superar el precio normal del mercado.</p>

    <form id="search-form">
      <input id="query" name="query" placeholder="Ejemplo: Alas de ónix" required />
      <button id="submit" type="submit">Buscar precio</button>
    </form>

    <div id="warnings"></div>

    <section id="summary" class="hidden">
      <div class="offer">
        <h2 id="sale-title"></h2>
        <p><span class="pill" id="sale-source"></span><span class="pill" id="sale-condition"></span><span class="pill" id="sale-isbn"></span></p>
        <p class="subtle">Mejor precio nuevo encontrado: <a id="sale-link" href="#" target="_blank" rel="noreferrer">ver libro</a></p>
      </div>

      <div class="grid">
        <div class="card"><h3>Precio de venta</h3><div class="value" id="sale-price"></div></div>
        <div class="card"><h3>Precio de compra</h3><div class="value" id="purchase-price"></div><div class="subtle">10% de descuento frente al precio de venta.</div></div>
        <div class="card"><h3>Utilidad</h3><div class="value" id="utility"></div><div class="subtle">Diferencia entre venta y compra.</div></div>
        <div class="card"><h3>Precio con envío</h3><div class="value" id="shipping-price"></div><div class="subtle">Precio de venta + $ 7.900.</div></div>
      </div>
    </section>

    <section id="market" class="section hidden">
      <h2>Referencia de mercado por ISBN</h2>
      <div class="grid">
        <div class="card"><h3>Precio normal de mercado</h3><div class="value" id="market-price"></div><div class="subtle">Referencia conservadora usando Tornamesa y Casa del Libro.</div></div>
        <div class="card"><h3>Precio sugerido</h3><div class="value" id="suggested-price"></div><div class="subtle">Solo se propone si mejora la utilidad sin superar el mercado.</div></div>
        <div class="card"><h3>Utilidad sugerida</h3><div class="value" id="suggested-utility"></div><div class="subtle">Calculada contra el precio de compra.</div></div>
      </div>
      <div id="market-offers"></div>
    </section>
  </main>

  <script>
    const money = (value) => value == null ? 'No disponible' : new Intl.NumberFormat('es-CO', { style: 'currency', currency: 'COP', maximumFractionDigits: 0 }).format(value);
    const form = document.getElementById('search-form');
    const submit = document.getElementById('submit');
    const warnings = document.getElementById('warnings');
    const summary = document.getElementById('summary');
    const market = document.getElementById('market');

    function renderWarnings(items) {
      warnings.innerHTML = '';
      items.forEach((item) => {
        const div = document.createElement('div');
        div.className = 'warning';
        div.textContent = item;
        warnings.appendChild(div);
      });
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      submit.textContent = 'Buscando...';
      warnings.innerHTML = '';
      summary.classList.add('hidden');
      market.classList.add('hidden');
      try {
        const query = document.getElementById('query').value.trim();
        const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
        const data = await response.json();
        renderWarnings(data.warnings || []);
        if (!data.sale_offer) {
          return;
        }

        summary.classList.remove('hidden');
        document.getElementById('sale-title').textContent = data.sale_offer.title;
        document.getElementById('sale-source').textContent = data.sale_offer.source;
        document.getElementById('sale-condition').textContent = data.sale_offer.condition || 'Estado no reconocido';
        document.getElementById('sale-isbn').textContent = data.sale_offer.isbn ? `ISBN ${data.sale_offer.isbn}` : 'ISBN no disponible';
        document.getElementById('sale-link').href = data.sale_offer.url;
        document.getElementById('sale-price').textContent = money(data.sale_offer.price);
        document.getElementById('purchase-price').textContent = money(data.purchase_price);
        document.getElementById('utility').textContent = money(data.utility);
        document.getElementById('shipping-price').textContent = money(data.shipping_price);

        market.classList.remove('hidden');
        document.getElementById('market-price').textContent = money(data.market_reference_price);
        document.getElementById('suggested-price').textContent = money(data.suggested_sale_price);
        document.getElementById('suggested-utility').textContent = money(data.suggested_utility);

        const marketOffers = document.getElementById('market-offers');
        marketOffers.innerHTML = '';
        (data.market_offers || []).forEach((offer) => {
          const article = document.createElement('article');
          article.className = 'offer';
          article.innerHTML = `
            <h3>${offer.title}</h3>
            <p><span class="pill">${offer.source}</span>${offer.isbn ? `<span class="pill">ISBN ${offer.isbn}</span>` : ''}</p>
            <p><strong>${money(offer.price)}</strong> · <a href="${offer.url}" target="_blank" rel="noreferrer">Abrir ficha</a></p>
          `;
          marketOffers.appendChild(article);
        });
      } catch (error) {
        renderWarnings([`Error inesperado: ${error.message}`]);
      } finally {
        submit.disabled = false;
        submit.textContent = 'Buscar precio';
      }
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/':
            body = INDEX_HTML.encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == '/api/search':
            params = urllib.parse.parse_qs(parsed.query)
            query = normalize_whitespace(params.get('q', [''])[0])
            if not query:
                self.send_error(HTTPStatus.BAD_REQUEST, 'Falta el término de búsqueda.')
                return
            try:
                payload = asdict(build_response(query))
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({'error': str(exc)}, ensure_ascii=False).encode('utf-8')
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        self.send_error(HTTPStatus.NOT_FOUND, 'Ruta no encontrada.')

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


if __name__ == '__main__':
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Servidor disponible en http://{HOST}:{PORT}')
    server.serve_forever()
