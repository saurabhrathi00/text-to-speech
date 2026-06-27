"""Razorpay self-serve checkout — orders, signature verify, webhook.

Source of truth for granting a plan is the webhook (payment.captured /
order.paid). The client-side `verify_payment` is a UX fast-path so the
user sees their upgrade instantly; both funnel through the idempotent
`_grant_order`, so a webhook + verify (or a webhook retry) never
double-credits.

Deliberately uses `requests` + stdlib `hmac` only — no razorpay SDK —
to keep the cloud requirements.txt lean (same spirit as llm/).

Env:
  RAZORPAY_KEY_ID         - public key id (also sent to the frontend)
  RAZORPAY_KEY_SECRET     - secret, used for Basic auth + verify signature
  RAZORPAY_WEBHOOK_SECRET - secret configured on the Razorpay webhook
"""
import os
import hmac
import json
import hashlib

import requests

import auth


API_BASE = "https://api.razorpay.com/v1"
_TIMEOUT = 20


class RazorpayError(Exception):
    pass


# ── Config ─────────────────────────────────────────────────────────────

def key_id() -> str:
    return os.getenv("RAZORPAY_KEY_ID", "")


def _key_secret() -> str:
    return os.getenv("RAZORPAY_KEY_SECRET", "")


def _webhook_secret() -> str:
    return os.getenv("RAZORPAY_WEBHOOK_SECRET", "")


def is_configured() -> bool:
    """True when checkout can run. Routes fall back to the manual
    upgrade-request flow when this is False (e.g. the local admin box)."""
    return bool(key_id() and _key_secret())


# ── Order creation ─────────────────────────────────────────────────────

def create_order(user_id: str, plan: str) -> tuple[dict | None, str | None]:
    """Create a Razorpay order for `plan` and persist a payment_orders
    row. Returns (checkout_payload, error). The payload is everything the
    frontend needs to open the Razorpay modal."""
    plan = (plan or "").lower().strip()
    if plan in ("", "free", "admin"):
        return None, f"Plan '{plan}' is not purchasable"

    limits = auth.get_plan_limits(plan)
    if not limits:
        return None, f"Unknown plan '{plan}'"
    price = int(limits.get("price_inr_monthly") or 0)
    if price <= 0:
        return None, f"Plan '{plan}' has no price set"

    amount_paise = price * 100
    try:
        r = requests.post(
            f"{API_BASE}/orders",
            auth=(key_id(), _key_secret()),
            json={
                "amount": amount_paise,
                "currency": "INR",
                "notes": {"user_id": user_id, "plan": plan},
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        return None, f"Payment gateway unreachable: {e}"
    if r.status_code not in (200, 201):
        print(f"[payments] order create failed {r.status_code}: {r.text[:300]}")
        return None, "Could not start checkout — try again"

    order = r.json()
    order_id = order.get("id")
    if not order_id:
        return None, "Payment gateway returned no order id"

    try:
        auth.admin_client().table("payment_orders").insert({
            "user_id": user_id,
            "plan": plan,
            "razorpay_order_id": order_id,
            "amount_paise": amount_paise,
            "currency": "INR",
            "status": "created",
        }).execute()
    except Exception as e:
        # Order exists at Razorpay but we couldn't persist it. The webhook
        # would have nothing to match against, so fail the checkout.
        print(f"[payments] failed to persist order {order_id}: {e}")
        return None, "Could not start checkout — try again"

    return {
        "key_id": key_id(),
        "order_id": order_id,
        "amount": amount_paise,
        "currency": "INR",
        "plan": plan,
        "display_name": limits.get("display_name") or plan,
    }, None


# ── Plan grant (idempotent, shared by verify + webhook) ────────────────

def _grant_order(order_id: str, payment_id: str | None) -> tuple[dict | None, str | None]:
    """Apply the order's plan to its user exactly once. Safe to call
    concurrently from both the client verify and the webhook.

    Idempotency is enforced by an ATOMIC claim: we flip `granted` from
    false→true in a single conditional UPDATE before doing any grant work,
    so only ONE caller (the winner of that update) ever reaches
    apply_plan_grant. Without this, a webhook + verify (or a webhook retry)
    racing inside the grant window would double-credit additive top-ups.
    """
    try:
        res = (auth.admin_client().table("payment_orders")
               .select("*").eq("razorpay_order_id", order_id).execute())
        rows = getattr(res, "data", None) or []
    except Exception as e:
        return None, f"Order lookup failed: {e}"
    if not rows:
        return None, "Order not found"
    order = rows[0]

    if order.get("granted"):
        return order, None  # already applied — idempotent no-op

    # Atomic claim: only the caller whose UPDATE actually matches a row
    # (granted still false) wins the right to apply the grant. Concurrent
    # callers get zero rows back and bail as a no-op.
    try:
        claim = (auth.admin_client().table("payment_orders").update({
            "status": "paid",
            "granted": True,
            "razorpay_payment_id": payment_id,
            "paid_at": "now()",
        }).eq("razorpay_order_id", order_id).eq("granted", False).execute())
        claimed = getattr(claim, "data", None) or []
    except Exception as e:
        return None, f"Order claim failed: {e}"
    if not claimed:
        return order, None  # someone else already claimed it — no double-credit

    ok, err = auth.apply_plan_grant(
        order["user_id"], order["plan"], source_ref=f"order:{order_id}")
    if not ok:
        # We claimed but the grant failed — release the claim so a webhook
        # retry can re-attempt cleanly instead of leaving paid-but-ungranted.
        try:
            auth.admin_client().table("payment_orders").update({
                "status": "created", "granted": False,
            }).eq("razorpay_order_id", order_id).execute()
        except Exception as e2:
            print(f"[payments] FAILED to release claim on {order_id}: {e2}")
        return None, err or "Failed to apply plan"

    return claimed[0], None


# ── Client-side verify (fast path) ─────────────────────────────────────

def verify_payment(user_id: str, order_id: str, payment_id: str,
                   signature: str) -> tuple[dict | None, str | None]:
    """Verify the checkout callback signature and grant the plan.
    Signature = HMAC_SHA256(key_secret, '{order_id}|{payment_id}')."""
    if not (order_id and payment_id and signature):
        return None, "Missing payment fields"
    expected = hmac.new(
        _key_secret().encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None, "Payment signature mismatch"

    order, err = _grant_order(order_id, payment_id)
    if err:
        return None, err
    # Defend against a tampered client claiming someone else's order.
    if order and order.get("user_id") != user_id:
        return None, "Order does not belong to this user"
    return {"plan": order.get("plan"), "status": "paid"}, None


# ── Webhook (source of truth) ──────────────────────────────────────────

def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    secret = _webhook_secret()
    if not (secret and signature):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def handle_webhook(raw_body: bytes, signature: str) -> tuple[bool, str | None]:
    """Process a Razorpay webhook. Returns (handled_ok, error). The
    caller should still return HTTP 200 on a verified-but-ignored event
    so Razorpay stops retrying."""
    if not verify_webhook_signature(raw_body, signature):
        return False, "Invalid webhook signature"

    try:
        body = json.loads(raw_body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return False, "Malformed webhook body"

    event = body.get("event") or ""
    payload = body.get("payload") or {}

    order_id = None
    payment_id = None
    if event == "payment.captured":
        entity = (payload.get("payment") or {}).get("entity") or {}
        order_id = entity.get("order_id")
        payment_id = entity.get("id")
    elif event == "order.paid":
        entity = (payload.get("order") or {}).get("entity") or {}
        order_id = entity.get("id")
        pay_entity = (payload.get("payment") or {}).get("entity") or {}
        payment_id = pay_entity.get("id")
    else:
        # Verified but not an event we act on — acknowledge so retries stop.
        return True, None

    if not order_id:
        return True, None  # nothing to match; acknowledge

    _, err = _grant_order(order_id, payment_id)
    if err:
        # Return non-200 so Razorpay retries (with backoff, over hours).
        # "Order not found" usually means the webhook beat create_order's
        # INSERT — a retry will land after the row exists. The webhook is
        # the source of truth, so we must NOT silently ACK-and-drop it.
        print(f"[payments] webhook grant error for {order_id}: {err}")
        return False, err
    return True, None
