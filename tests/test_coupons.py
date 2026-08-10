"""Coupon math, validity, pricing, and admin-validation."""
import coupons


def _c(**kw):
    base = {"code": "X", "discount_type": "percent", "discount_value": 50,
            "active": True, "used_count": 0, "max_uses": None,
            "expires_at": None, "applies_to": None, "auto_apply": False}
    base.update(kw)
    return base


# ── discount math ────────────────────────────────────────────────────
def test_percent_discount():
    assert coupons.discount_for(_c(discount_value=50), "pro", 99900) == 49950


def test_flat_discount_in_paise():
    assert coupons.discount_for(_c(discount_type="flat", discount_value=20000), "pro", 99900) == 20000


def test_discount_never_below_one_rupee():
    # 100% off a ₹5 plan must still leave the 100-paise Razorpay minimum.
    d = coupons.discount_for(_c(discount_value=100), "pro", 500)
    assert d == 400 and (500 - d) == 100


def test_flat_larger_than_price_is_clamped():
    d = coupons.discount_for(_c(discount_type="flat", discount_value=999999), "pro", 50000)
    assert (50000 - d) == 100


def test_inactive_coupon_gives_no_discount():
    assert coupons.discount_for(_c(active=False), "pro", 99900) == 0


# ── validity ─────────────────────────────────────────────────────────
def test_valid_now_true():
    assert coupons.is_valid_now(_c()) is True


def test_expired_coupon_invalid():
    assert coupons.is_valid_now(_c(expires_at="2000-01-01T00:00:00Z")) is False


def test_maxed_out_coupon_invalid():
    assert coupons.is_valid_now(_c(max_uses=5, used_count=5)) is False


def test_none_coupon_invalid():
    assert coupons.is_valid_now(None) is False


# ── applies_to ───────────────────────────────────────────────────────
def test_applies_to_all_when_empty():
    assert coupons.applies_to_plan(_c(applies_to=None), "pro") is True


def test_applies_to_restricted():
    c = _c(applies_to=["starter"])
    assert coupons.applies_to_plan(c, "starter") is True
    assert coupons.applies_to_plan(c, "pro") is False


# ── validate_coupon (DB-mocked) ──────────────────────────────────────
def test_validate_unknown_code(monkeypatch):
    monkeypatch.setattr(coupons, "get_coupon", lambda code: None)
    disc, c, err = coupons.validate_coupon("NOPE", "pro", 99900)
    assert disc == 0 and c is None and err


def test_validate_good_code(monkeypatch):
    monkeypatch.setattr(coupons, "get_coupon", lambda code: _c(code="LAUNCH50"))
    disc, c, err = coupons.validate_coupon("LAUNCH50", "pro", 99900)
    assert err is None and disc == 49950 and c["code"] == "LAUNCH50"


def test_validate_wrong_plan(monkeypatch):
    monkeypatch.setattr(coupons, "get_coupon", lambda code: _c(applies_to=["starter"]))
    disc, c, err = coupons.validate_coupon("X", "pro", 99900)
    assert disc == 0 and "apply" in err.lower()


# ── resolve_for_checkout ─────────────────────────────────────────────
def test_resolve_prefers_explicit_code(monkeypatch):
    monkeypatch.setattr(coupons, "get_coupon", lambda code: _c(code="CODE20", discount_value=20))
    monkeypatch.setattr(coupons, "get_auto_apply_coupon", lambda: _c(code="AUTO50", discount_value=50))
    disc, code = coupons.resolve_for_checkout("CODE20", "pro", 100000)
    assert code == "CODE20" and disc == 20000


def test_resolve_falls_back_to_auto(monkeypatch):
    monkeypatch.setattr(coupons, "get_auto_apply_coupon", lambda: _c(code="AUTO50", discount_value=50))
    disc, code = coupons.resolve_for_checkout(None, "pro", 100000)
    assert code == "AUTO50" and disc == 50000


def test_resolve_none_when_no_coupon(monkeypatch):
    monkeypatch.setattr(coupons, "get_auto_apply_coupon", lambda: None)
    assert coupons.resolve_for_checkout(None, "pro", 100000) == (0, None)


# ── effective_price (pricing display) ────────────────────────────────
def test_effective_price_with_auto():
    p = coupons.effective_price("pro", 99900, auto=_c(code="LAUNCH50", discount_value=50))
    assert p["effective_paise"] == 49950 and p["off_pct"] == 50 and p["coupon"] == "LAUNCH50"


def test_effective_price_no_coupon():
    p = coupons.effective_price("pro", 99900, auto=None)
    assert p["effective_paise"] == 99900 and p["discount_paise"] == 0 and p["coupon"] is None


# ── admin upsert validation ──────────────────────────────────────────
def test_upsert_rejects_empty_code():
    _, err = coupons.upsert_coupon({"code": "", "discount_value": 10})
    assert err


def test_upsert_rejects_bad_percent():
    _, err = coupons.upsert_coupon({"code": "X", "discount_type": "percent", "discount_value": 150})
    assert err and "1" in err


def test_upsert_rejects_non_numeric_value():
    _, err = coupons.upsert_coupon({"code": "X", "discount_value": "abc"})
    assert err
