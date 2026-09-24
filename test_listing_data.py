import unittest

from listing_data import indexed_inactive, page_details, schema_offer, snippet_price


class ListingDataTests(unittest.TestCase):
    def test_removed_vinted_listing_is_rejected_even_when_schema_says_in_stock(self):
        url = 'https://www.vinted.es/items/2594995751-levis-501-vintage'
        page = '''<script type="application/ld+json">{"@type":"Product","offers":
            {"@type":"Offer","url":"https://www.vinted.es/items/2594995751-levis-501-vintage",
             "priceCurrency":"EUR","price":25,"availability":"InStock"}}</script>
            <span aria-live="polite" class="u-visually-hidden">¡Eliminado!</span>'''
        self.assertEqual(page_details(page, url), (25.0, True))

    def test_product_price_and_currency_are_scoped_to_listing(self):
        url = 'https://www.vinted.fi/items/123-jeans'
        schemas = [
            {'@type':'Product','offers':{'@type':'Offer','url':'https://www.vinted.fi/items/999-other','priceCurrency':'EUR','price':5}},
            {'@type':'Product','offers':{'@type':'Offer','url':url,'priceCurrency':'GBP','price':12}},
            {'@type':'Product','offers':{'@type':'Offer','url':url,'priceCurrency':'EUR','price':'35,50'}},
        ]
        self.assertEqual(schema_offer(schemas,url), (35.5,False))

    def test_shipping_fee_is_not_mistaken_for_item_price(self):
        self.assertIsNone(snippet_price('Shipping from 3,99 € · buyer protection 1,95 €'))
        self.assertEqual(snippet_price('Levi’s 501 · 25,00 € · shipping from 3,99 €'),25.0)

    def test_open_graph_price_and_inactive_snippet(self):
        page = '<meta property="product:price:currency" content="EUR"><meta property="product:price:amount" content="49.00">'
        self.assertEqual(page_details(page, 'https://depop.com/products/jeans'),(49.0,False))
        self.assertTrue(indexed_inactive({'title':'Levi’s jeans SOLD','description':''}))
        self.assertFalse(indexed_inactive({'title':'Unsold vintage jeans','description':''}))


if __name__ == '__main__':
    unittest.main()
