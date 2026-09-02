"""Thin client for the CRM sandbox API."""
import time
from typing import Dict, List, Optional

import requests

from . import config


class CRMError(RuntimeError):
    pass


class CRM:
    def __init__(self, token: Optional[str] = None, base: Optional[str] = None):
        self.base = (base or config.CRM_BASE).rstrip("/")
        self.token = token or config.CRM_TOKEN
        if not self.token:
            raise CRMError("CRM_TOKEN is not set (put it in .env or the environment)")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})

    def _req(self, method: str, path: str, **kw):
        url = self.base + path
        for attempt in range(3):
            r = self.s.request(method, url, timeout=30, **kw)
            if r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json().get("detail")
                except Exception:
                    detail = r.text
                raise CRMError(f"{method} {path} -> {r.status_code}: {detail}")
            return r.json()
        raise CRMError(f"{method} {path} failed after retries: {r.status_code}")

    # ---- reads
    def me(self) -> Dict:
        return self._req("GET", "/me")

    def list_accounts(self, **filters) -> List[Dict]:
        out, page = [], 1
        while True:
            d = self._req("GET", "/accounts", params={**filters, "page": page, "page_size": 100})
            out.extend(d["data"])
            if not d["data"] or len(out) >= d.get("total", 0):
                break
            page += 1
        return out

    def get_account(self, account_id: str) -> Dict:
        return self._req("GET", f"/accounts/{account_id}")

    def list_contacts(self, **filters) -> List[Dict]:
        out, page = [], 1
        while True:
            d = self._req("GET", "/contacts", params={**filters, "page": page, "page_size": 100})
            out.extend(d["data"])
            if not d["data"] or len(out) >= d.get("total", 0):
                break
            page += 1
        return out

    # ---- writes (only ever called from apply.py after human approval)
    def create_account(self, body: Dict) -> Dict:
        return self._req("POST", "/accounts", json=body)

    def update_account(self, account_id: str, body: Dict) -> Dict:
        return self._req("PATCH", f"/accounts/{account_id}", json=body)

    def get_contact(self, contact_id: str) -> Dict:
        return self._req("GET", f"/contacts/{contact_id}")

    def create_contact(self, body: Dict) -> Dict:
        return self._req("POST", "/contacts", json=body)

    def update_contact(self, contact_id: str, body: Dict) -> Dict:
        return self._req("PATCH", f"/contacts/{contact_id}", json=body)
