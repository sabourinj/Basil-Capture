"""Thin Grocy REST client for barcode-based stock consumption and its undo.

Verified against the Grocy 4.6.0 OpenAPI spec:
  POST /stock/products/by-barcode/{barcode}/consume
    body: {"amount": <num>, "transaction_type": "consume", "spoiled": <bool>}
    200 -> JSON ARRAY of booking objects
    400 -> {"error_message": "..."}  (unknown barcode, amount > stock, etc.)
  GET  /stock/products/by-barcode/{barcode}  -> {"product": {"name": ...}, ...}

Added for undo (endpoint is listed in Grocy's OpenAPI spec; NOT yet exercised
against a live 4.6.0 instance, so these two points still need a real test):
  POST /stock/transactions/{transactionId}/undo
    reverts every booking in the transaction at once. A single consume can span
    several stock entries, which makes several bookings, so undoing the
    transaction is the one call that puts everything back.
  The consume response's booking objects are expected to carry `transaction_id`.
  If they don't, consume_by_barcode returns None for it and the Indicator never
  shows an Undo button (it fails closed rather than offering a dead button).

Thread safety: the undo runs on an MQTT worker thread while the main loop may be
consuming, and requests.Session isn't documented as thread-safe, so every HTTP
call goes through one lock.
"""
import sys
import threading
from urllib.parse import quote

import requests


class GrocyError(Exception):
    """Raised on a 400 or network failure. `.user_message` is display-safe.

    `.server_side` is True when Grocy couldn't be reached or failed itself
    (timeout, network, non-400 status) rather than refusing the request (400:
    unknown barcode, out of stock). The scanner beeps differently for each, so
    "try again later" never sounds like "this item is wrong".
    """
    def __init__(self, message, user_message=None, server_side=False):
        super().__init__(message)
        self.user_message = user_message or message
        self.server_side = server_side


class GrocyClient:
    def __init__(self, base_url, api_key, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "GROCY-API-KEY": api_key,
            "Content-Type": "application/json",
        })
        self.timeout = timeout
        self._lock = threading.Lock()

    def consume_by_barcode(self, barcode, amount=1, spoiled=False):
        """Consume stock by barcode.

        Returns (product_name, consumed_amount, remaining, transaction_id) on
        success, where `remaining` is the stock left AFTER this consume, or None
        if the stock lookup failed. None and 0 mean different things - see
        _product_details. `transaction_id` is a string, or None if Grocy's
        response didn't include one (no undo is offered in that case).

        Raises GrocyError on failure.
        """
        url = f"{self.base_url}/stock/products/by-barcode/{barcode}/consume"
        payload = {
            "amount": amount,
            "transaction_type": "consume",
            "spoiled": spoiled,
        }
        try:
            with self._lock:
                r = self.session.post(url, json=payload, timeout=self.timeout)
        except requests.Timeout:
            raise GrocyError("Timeout", "Server timeout", server_side=True)
        except requests.RequestException as e:
            raise GrocyError(f"Network error: {e}", "Network error", server_side=True)

        if r.status_code == 200:
            # 200 body is an ARRAY of booking objects.
            try:
                bookings = r.json()
            except ValueError:
                bookings = []
            consumed = sum(abs(b.get("amount", 0)) for b in bookings) or amount
            transaction_id = self._transaction_id(bookings)
            if transaction_id is None:
                print("  warning: consume response had no transaction_id; "
                      "no undo will be offered for this scan", file=sys.stderr)
            name, remaining = self._product_details(barcode)
            return name, consumed, remaining, transaction_id

        if r.status_code == 400:
            msg = self._err(r)
            raise GrocyError(msg, self._friendly_400(msg))

        raise GrocyError(f"HTTP {r.status_code}", f"Error {r.status_code}",
                         server_side=True)

    def undo_transaction(self, transaction_id):
        """POST /stock/transactions/{id}/undo - revert a whole consume.

        Raises GrocyError on failure (already undone, unknown id, network).
        """
        url = (f"{self.base_url}/stock/transactions/"
               f"{quote(str(transaction_id), safe='')}/undo")
        try:
            with self._lock:
                r = self.session.post(url, timeout=self.timeout)
        except requests.Timeout:
            raise GrocyError("Timeout", "Server timeout", server_side=True)
        except requests.RequestException as e:
            raise GrocyError(f"Network error: {e}", "Network error", server_side=True)

        # Grocy answers a successful undo with an empty body; accept 200 or 204.
        if r.status_code in (200, 204):
            return

        if r.status_code == 400:
            msg = self._err(r)
            raise GrocyError(msg, self._friendly_undo_400(msg))

        raise GrocyError(f"HTTP {r.status_code}", f"Error {r.status_code}",
                         server_side=True)

    @staticmethod
    def _transaction_id(bookings):
        """First transaction_id found in the booking objects, as a string."""
        for b in bookings:
            tid = b.get("transaction_id") if isinstance(b, dict) else None
            if tid:
                return str(tid)
        return None

    def _product_details(self, barcode):
        """GET /stock/products/by-barcode/{barcode} -> (product.name, stock_amount)

        Called AFTER the consume POST, so stock_amount is what's left rather
        than what was there before.

        Falls back to (barcode, None) on any failure. None is deliberately
        distinct from 0: 0 means "you just took the last one", None means "we
        couldn't find out", and the display shows nothing at all for None
        rather than claiming the shelf is empty.
        """
        try:
            url = f"{self.base_url}/stock/products/by-barcode/{barcode}"
            with self._lock:
                r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                data = r.json()
                name = data.get("product", {}).get("name", barcode)
                return name, self._to_number(data.get("stock_amount"))
        except requests.RequestException:
            pass
        return barcode, None

    @staticmethod
    def _to_number(raw):
        """Grocy returns stock amounts as strings ("3", "1.5"). None on junk."""
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

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

    @staticmethod
    def _friendly_undo_400(msg):
        low = msg.lower()
        if "undone" in low:
            return "Already undone"
        return msg[:40]
