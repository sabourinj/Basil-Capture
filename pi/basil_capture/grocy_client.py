"""Thin Grocy REST client for barcode-based stock consumption.

Verified against the Grocy 4.6.0 OpenAPI spec:
  POST /stock/products/by-barcode/{barcode}/consume
    body: {"amount": <num>, "transaction_type": "consume", "spoiled": <bool>}
    200 -> JSON ARRAY of booking objects
    400 -> {"error_message": "..."}  (unknown barcode, amount > stock, etc.)
  GET  /stock/products/by-barcode/{barcode}  -> {"product": {"name": ...}, ...}
"""
import requests


class GrocyError(Exception):
    """Raised on a 400 or network failure. `.user_message` is display-safe."""
    def __init__(self, message, user_message=None):
        super().__init__(message)
        self.user_message = user_message or message


class GrocyClient:
    def __init__(self, base_url, api_key, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "GROCY-API-KEY": api_key,
            "Content-Type": "application/json",
        })
        self.timeout = timeout

    def consume_by_barcode(self, barcode, amount=1, spoiled=False):
        """Consume stock by barcode.

        Returns (product_name, consumed_amount) on success.
        Raises GrocyError on failure.
        """
        url = f"{self.base_url}/stock/products/by-barcode/{barcode}/consume"
        payload = {
            "amount": amount,
            "transaction_type": "consume",
            "spoiled": spoiled,
        }
        try:
            r = self.session.post(url, json=payload, timeout=self.timeout)
        except requests.Timeout:
            raise GrocyError("Timeout", "Server timeout")
        except requests.RequestException as e:
            raise GrocyError(f"Network error: {e}", "Network error")

        if r.status_code == 200:
            # 200 body is an ARRAY of booking objects.
            try:
                bookings = r.json()
            except ValueError:
                bookings = []
            consumed = sum(abs(b.get("amount", 0)) for b in bookings) or amount
            return self._product_name(barcode), consumed

        if r.status_code == 400:
            msg = self._err(r)
            raise GrocyError(msg, self._friendly_400(msg))

        raise GrocyError(f"HTTP {r.status_code}", f"Error {r.status_code}")

    def _product_name(self, barcode):
        """GET /stock/products/by-barcode/{barcode} -> product.name"""
        try:
            url = f"{self.base_url}/stock/products/by-barcode/{barcode}"
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                return r.json().get("product", {}).get("name", barcode)
        except requests.RequestException:
            pass
        return barcode

    @staticmethod
    def _err(response):
        try:
            return response.json().get("error_message", "Bad request")
        except ValueError:
            return "Bad request"

    @staticmethod
    def _friendly_400(msg):
        low = msg.lower()
        if "amount" in low and "stock" in low:
            return "Out of stock"
        if "not existing" in low or "no product" in low:
            return "Unknown barcode"
        return msg[:40]
