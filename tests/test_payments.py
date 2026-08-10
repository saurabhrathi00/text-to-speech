"""Razorpay signature verification (checkout callback + webhook)."""
import hashlib
import hmac
import json

import payments


SECRET = "test_secret_123"
WEBHOOK_SECRET = "whsec_test_456"


def _checkout_sig(order_id, payment_id, secret=SECRET):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(),
                    hashlib.sha256).hexdigest()


def _webhook_sig(body: bytes, secret=WEBHOOK_SECRET):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── verify_payment ────────────────────────────────────────────────────
def test_verify_missing_fields(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
    _, err = payments.verify_payment("u1", "", "pay_1", "sig")
    assert err and "missing" in err.lower()


def test_verify_bad_signature(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
    _, err = payments.verify_payment("u1", "order_1", "pay_1", "deadbeef")
    assert err and "signature" in err.lower()


def test_verify_good_signature_grants(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
    monkeypatch.setattr(payments, "_grant_order",
                        lambda oid, pid: ({"plan": "pro", "user_id": "u1"}, None))
    sig = _checkout_sig("order_1", "pay_1")
    res, err = payments.verify_payment("u1", "order_1", "pay_1", sig)
    assert err is None and res["plan"] == "pro" and res["status"] == "paid"


def test_verify_rejects_wrong_owner(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", SECRET)
    monkeypatch.setattr(payments, "_grant_order",
                        lambda oid, pid: ({"plan": "pro", "user_id": "someone_else"}, None))
    sig = _checkout_sig("order_1", "pay_1")
    _, err = payments.verify_payment("u1", "order_1", "pay_1", sig)
    assert err and "belong" in err.lower()


# ── webhook signature ────────────────────────────────────────────────
def test_webhook_signature_valid(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = b'{"event":"x"}'
    assert payments.verify_webhook_signature(body, _webhook_sig(body)) is True


def test_webhook_signature_invalid(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    assert payments.verify_webhook_signature(b'{"event":"x"}', "bad") is False


def test_webhook_signature_no_secret(monkeypatch):
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
    assert payments.verify_webhook_signature(b'{}', "whatever") is False


# ── handle_webhook ───────────────────────────────────────────────────
def test_handle_webhook_rejects_bad_sig(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    ok, err = payments.handle_webhook(b'{"event":"payment.captured"}', "bad")
    assert ok is False and err


def test_handle_webhook_ignores_unknown_event(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = json.dumps({"event": "subscription.charged"}).encode()
    ok, err = payments.handle_webhook(body, _webhook_sig(body))
    assert ok is True and err is None  # verified but not acted on


def test_handle_webhook_captured_grants(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    seen = {}
    monkeypatch.setattr(payments, "_grant_order",
                        lambda oid, pid: (seen.update(order_id=oid, payment_id=pid) or ({}, None)))
    body = json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_9", "order_id": "order_9"}}},
    }).encode()
    ok, err = payments.handle_webhook(body, _webhook_sig(body))
    assert ok is True and seen == {"order_id": "order_9", "payment_id": "pay_9"}
