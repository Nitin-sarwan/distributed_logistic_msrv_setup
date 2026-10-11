# Payment Service

Whether an order's money has arrived, and how. Port **8007**, database
**payment_db**. It replaced the stub inside orderServices
(`PAYMENT_AUTOCONFIRM`).

---

## How a customer pays

Chosen at booking, stored on the order as `payment_method`:

| | Pay online now | Pay on delivery |
| --- | --- | --- |
| When | At confirm, before a driver is sought | At drop-off, to the driver |
| How | A provider — **simulated** until one is connected | Cash, or UPI by scanning the driver's QR |
| Order confirmed | Once paid | Immediately; money is due later |
| Driver sees | "Paid online · don't collect anything" | "Collect ₹X" → **Cash received** / **Show UPI QR** |

```
confirm ──HTTP──▶ Payment  POST /internal/payments   (opens the payment)
                     │
   online + autocapture ─▶ paid now ─▶ payment.captured
   on_delivery          ─▶ awaiting
                     │
driver at the door ──▶ POST /api/deliveries/payments/{order}/collect {via: cash|upi}
                     └─▶ paid ─▶ payment.captured ──Kafka──▶ Order: payment_status=paid
                                                             DELIVERED → SETTLED
                                                  └────────▶ Notification: receipt
```

**Delivered is not settled.** Order moves to SETTLED ("Completed") only when
both are true — goods handed over and money in — whichever happens second.

---

## The UPI QR

`GET /api/deliveries/payments/{order}/upi` returns a standard NPCI link,
`upi://pay?pa=…&pn=…&am=92.40&cu=INR&tr=LP-XXXXXX`, which the driver's app draws
as a QR. Any UPI app reads it, with the amount and reference pre-filled.

**Nothing verifies the payment.** The driver taps "Payment received" after
seeing it succeed on the customer's phone. That was the deliberate choice for
now; it is accountable rather than verified — who collected, how and when is
recorded on a row nobody edits. A provider's webhook (Razorpay QR) is what makes
it verified, and slots in by emitting the same `payment.captured`.

**`PAYMENT_UPI_VPA` must be the business's real UPI ID** before any customer
scans a QR. The default, `logisticpartner@upi`, is a placeholder; the service
logs a warning and reports `upi_vpa_is_placeholder` in `/health` while it is set.

---

## Who may do what

- Only the **driver assigned to the order** may collect or fetch the QR — learnt
  from `partner.assigned`, checked in the SQL. Anyone else gets 404.
- A driver may record **cash or UPI only**, never "online".
- Collecting twice the same way is a no-op; collecting on a prepaid or cancelled
  order is 409.
- Opening a payment is internal-only (`/internal/payments`, not routed by the
  gateway): a customer who could open their own could mark it paid.

## Cancellation

`order.cancelled` → an unpaid payment is **voided**; one paid online is
**refunded** (simulated, like the payment) and `payment.refunded` tells Order
and the customer. An order cannot be cancelled after pickup, so cash and UPI
collections are never reversed here.

## Settings

| Setting | Default | |
| --- | --- | --- |
| `PAYMENT_DB_NAME` | `payment_db` | |
| `PAYMENT_ONLINE_AUTOCAPTURE` | `true` | "Pay online now" succeeds with no provider. **Must be false anywhere real** |
| `PAYMENT_UPI_VPA` | `logisticpartner@upi` | Placeholder — set the business's UPI ID |
| `PAYMENT_UPI_PAYEE_NAME` | `LogisticPartner` | Shown in the customer's UPI app |
| `PAYMENT_SERVICE_URL` | `http://127.0.0.1:8007` | Read by the gateway and Order |

```powershell
alembic -c alembic_payment.ini upgrade head     # -> b7e2d94a1c63
alembic -c alembic_order.ini upgrade head       # -> e4c7a2f91b05 (orders.payment_*)
uvicorn src.services.paymentServices.main:app --port 8007 --reload
```

## Not built

- A real provider for online payments and verified UPI (Razorpay), and its
  webhook route.
- Driver cash balances and payouts: what each driver holds and owes.
- Real refunds.
