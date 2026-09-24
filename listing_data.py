"""Conservative extraction of EUR listing prices and inactive signals."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlparse


INACTIVE_WORDS = re.compile(
    r"\b(?:sold(?:\s+out)?|reserved|myyty|varattu|poistettu|"
    r"vendido|vendu|verkauft|eliminado|supprimé|deleted|removed|"
    r"unavailable|expired|listing\s+ended)\b", re.I
)
def eur_amount(value):
    """Return a plausible EUR amount; never reinterpret another currency as EUR."""
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        raw = re.sub(r"[^0-9,.]", "", str(value or ""))
        if not raw:
            return None
        if ',' in raw and '.' in raw:
            raw = raw.replace('.', '').replace(',', '.') if raw.rfind(',') > raw.rfind('.') else raw.replace(',', '')
        else:
            raw = raw.replace(',', '.')
        try:
            amount = float(raw)
        except ValueError:
            return None
    return round(amount, 2) if 2 <= amount <= 1500 else None


def _types(obj):
    values = obj.get('@type', [])
    return {str(x).lower() for x in (values if isinstance(values, list) else [values])}


def schema_offer(schemas, listing_url=''):
    """Read only this listing's Product/Offer, never a recommendation's price."""
    def walk(value):
        if isinstance(value, list):
            for child in value:
                yield from walk(child)
        elif isinstance(value, dict):
            if 'product' in _types(value):
                for offer in walk(value.get('offers', [])):
                    yield offer
            elif 'offer' in _types(value) or 'priceCurrency' in value and 'price' in value:
                yield value
            for key in ('@graph', 'mainEntity'):
                yield from walk(value.get(key, []))

    for offer in walk(schemas or []):
        if not isinstance(offer, dict) or str(offer.get('priceCurrency', '')).upper() != 'EUR':
            continue
        offer_url = str(offer.get('url', ''))
        if listing_url and offer_url:
            wanted, offered = urlparse(listing_url), urlparse(offer_url)
            if wanted.path.rstrip('/') != offered.path.rstrip('/'):
                continue
        price = eur_amount(offer.get('price'))
        if price is not None:
            availability = str(offer.get('availability', '')).lower()
            inactive = any(x in availability for x in ('outofstock', 'soldout', 'discontinued'))
            return price, inactive
    return None, False


EURO_PRICE = re.compile(r'(?:€\s*(\d{1,4}(?:[.,]\d{1,2})?)|\bEUR\s*(\d{1,4}(?:[.,]\d{1,2})?)|\b(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|EUR\b))', re.I)
SHIPPING_WORDS = re.compile(r'(?:shipping|delivery|postage|buyer\s+protection|toimitus|lähetys|env[ií]o|protection|protecci[oó]n|desde|frais\s+de\s+port)', re.I)


def snippet_price(text):
    """Accept an explicit euro amount, excluding amounts labelled as fees/shipping."""
    text = str(text or '')
    for match in EURO_PRICE.finditer(text):
        left = text[max(0, match.start()-32):match.start()]
        if SHIPPING_WORDS.search(left.split('·')[-1].split('|')[-1]):
            continue
        amount = eur_amount(next(x for x in match.groups() if x is not None))
        if amount is not None:
            return amount
    return None


class _ListingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld = []
        self.meta = {}
        self.status_labels = []
        self._script = False
        self._status = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
            self._script = True
        if tag == 'meta':
            key = attrs.get('property') or attrs.get('name')
            if key:
                self.meta[key.lower()] = attrs.get('content', '')
        if tag in ('span', 'div') and attrs.get('aria-live') == 'polite':
            self._status = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self._script = False
        if tag in ('span', 'div'):
            self._status = False

    def handle_data(self, data):
        if self._script:
            try:
                self.jsonld.append(json.loads(data))
            except ValueError:
                pass
        if self._status:
            self.status_labels.append(data)


def page_details(page, listing_url):
    parser = _ListingParser()
    parser.feed(page[:1_500_000])
    price, unavailable = schema_offer(parser.jsonld, listing_url)
    if price is None:
        currency = parser.meta.get('product:price:currency', '').upper()
        if currency == 'EUR':
            price = eur_amount(parser.meta.get('product:price:amount'))
    labels = ' '.join(parser.status_labels+[parser.meta.get('og:title', '')])
    unavailable |= bool(INACTIVE_WORDS.search(labels))
    return price, unavailable


def indexed_inactive(result):
    visible = ' '.join(str(result.get(key) or '') for key in ('title', 'description'))
    return bool(INACTIVE_WORDS.search(visible))
