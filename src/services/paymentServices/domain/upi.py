"""The UPI payment link a driver shows as a QR code.

`upi://pay?...` is NPCI's standard deep link. Every UPI app — GPay, PhonePe,
Paytm, a bank's own — reads the same parameters, so one QR works for whichever
app the customer has, and the amount and reference arrive pre-filled.

Nothing here talks to a bank. The link only says where to pay and how much;
whether the money arrived is what the driver confirms afterwards. That is the
trade the platform chose for now, and what a provider's webhook replaces later.
"""

from decimal import Decimal
from urllib.parse import quote, urlencode


def upi_link(vpa: str, payee_name: str, amount: Decimal, reference: str) -> str:
    params = {
        "pa": vpa,  # payee address — the business's UPI ID
        "pn": payee_name,  # payee name shown in the customer's app
        "am": f"{amount:.2f}",  # amount, fixed so it cannot be edited down
        "cu": "INR",
        "tr": reference,  # transaction reference: the order's public ref
        "tn": f"Delivery {reference}",  # note the customer sees
    }
    # quote_via=quote so spaces become %20, which every UPI app parses; "+" is
    # read literally by some of them.
    # "@" stays literal: some apps do not decode %40 in the payee address.
    return "upi://pay?" + urlencode(params, quote_via=quote, safe="@")
