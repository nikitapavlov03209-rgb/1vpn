import httpx

class CryptoBot:
    def __init__(self, token: str, payee: str, base: str = "https://pay.crypt.bot/api"):
        self.token = token
        self.payee = payee
        self.base = base
        self.client = httpx.AsyncClient(timeout=20.0)

    async def create_invoice(self, amount: float, asset: str, payload: str, description: str, return_url: str):
        r = await self.client.post(f"{self.base}/createInvoice", json={
            "token": self.token,
            "asset": asset,
            "amount": str(amount),
            "description": description,
            "payload": payload,
            "paid_btn_name": "callback",
            "paid_btn_url": return_url
        })
        r.raise_for_status()
        data = r.json()
        return data["result"]["pay_url"], data["result"]["invoice_id"]

    async def get_invoice(self, invoice_id: int):
        r = await self.client.post(f"{self.base}/getInvoices", json={"token": self.token, "invoice_ids": [invoice_id]})
        r.raise_for_status()
        data = r.json()
        items = data.get("result", {}).get("items", [])
        return items[0] if items else None
