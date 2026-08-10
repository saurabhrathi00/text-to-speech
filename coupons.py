"""Coupon codes + pricing-psychology helpers.

Codes are never exposed to the browser. The backend:
  - computes effective (discounted) prices for /api/plans using the single
    active `auto_apply` coupon, so the pricing UI can show a struck-through
    list price and a lower "you pay" price;
  - validates + applies a coupon at checkout (payments.create_order);
  - offers admin CRUD.

Service-role only (auth.admin_client bypasses RLS). No new deps.
"""
from datetime import datetime, timezone

import auth


def _client():
    return auth.admin_client()


def _now_utc():
    return datetime.now(timezone.utc)


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ── Lookup ──────────────────────────────────────────────────────────────

def get_coupon(code: str) -> dict | None:
    code = (code or "").upper().strip()
    if not code:
        return None
    try:
        res = _client().table("coupons").select("*").eq("code", code).execute()
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[coupons] get_coupon failed: {e}")
        return None


def get_auto_apply_coupon() -> dict | None:
    """The single active auto-apply coupon used for the pre-applied pricing
    display. If several exist, the newest valid one wins."""
    try:
        res = (_client().table("coupons").select("*")
               .eq("auto_apply", True).eq("active", True)
               .order("created_at", desc=True).execute())
        for c in (getattr(res, "data", None) or []):
            if is_valid_now(c):
                return c
    except Exception as e:
        print(f"[coupons] get_auto_apply_coupon failed: {e}")
    return None


# ── Validity + discount math ────────────────────────────────────────────

def is_valid_now(c: dict | None) -> bool:
    if not c or not c.get("active"):
        return False
    exp = _parse_ts(c.get("expires_at"))
    if exp and exp < _now_utc():
        return False
    mx = c.get("max_uses")
    if mx is not None and int(c.get("used_count") or 0) >= int(mx):
        return False
    return True


def applies_to_plan(c: dict, plan: str) -> bool:
    ap = c.get("applies_to")
    return (not ap) or (plan in ap)


def discount_for(c: dict, plan: str, amount_paise: int) -> int:
    """Discount in paise for coupon c on this plan/amount (0 if N/A). Never
    drops the payable amount below ₹1 (Razorpay's 100-paise minimum)."""
    if not is_valid_now(c) or not applies_to_plan(c, plan):
        return 0
    dt = (c.get("discount_type") or "percent").lower()
    dv = int(c.get("discount_value") or 0)
    disc = (amount_paise * dv // 100) if dt == "percent" else dv
    return max(0, min(disc, amount_paise - 100))


def validate_coupon(code: str, plan: str, amount_paise: int):
    """Returns (discount_paise, coupon_row, error). error is user-facing."""
    c = get_coupon(code)
    if not c:
        return 0, None, "Invalid coupon code"
    if not is_valid_now(c):
        return 0, None, "This coupon is expired or fully used"
    if not applies_to_plan(c, plan):
        return 0, None, "This coupon doesn't apply to that plan"
    disc = discount_for(c, plan, amount_paise)
    if disc <= 0:
        return 0, None, "This coupon gives no discount on that plan"
    return disc, c, None


def resolve_for_checkout(code: str | None, plan: str, amount_paise: int):
    """Pick the coupon to actually charge with. An explicit code wins; else
    fall back to the auto-apply coupon so the customer pays the shown price.
    Returns (discount_paise, coupon_code_or_None)."""
    if code:
        disc, c, _ = validate_coupon(code, plan, amount_paise)
        if disc > 0:
            return disc, c["code"]
        return 0, None
    auto = get_auto_apply_coupon()
    if auto and applies_to_plan(auto, plan):
        disc = discount_for(auto, plan, amount_paise)
        if disc > 0:
            return disc, auto["code"]
    return 0, None


def effective_price(plan: str, base_paise: int, auto: dict | None = "unset") -> dict:
    """Pricing-display helper: apply the auto-apply coupon to a plan's list
    price. Pass `auto` once (from get_auto_apply_coupon) to avoid a DB hit per
    plan in a loop."""
    if auto == "unset":
        auto = get_auto_apply_coupon()
    if not auto or not applies_to_plan(auto, plan):
        return {"original_paise": base_paise, "effective_paise": base_paise,
                "discount_paise": 0, "coupon": None, "off_pct": 0}
    disc = discount_for(auto, plan, base_paise)
    off_pct = round(disc * 100 / base_paise) if base_paise else 0
    return {"original_paise": base_paise, "effective_paise": base_paise - disc,
            "discount_paise": disc, "coupon": auto.get("code"), "off_pct": off_pct}


def increment_use(code: str | None):
    if not code:
        return
    c = get_coupon(code)
    if not c:
        return
    try:
        _client().table("coupons").update(
            {"used_count": int(c.get("used_count") or 0) + 1}
        ).eq("code", c["code"]).execute()
    except Exception as e:
        print(f"[coupons] increment_use failed: {e}")


# ── Admin CRUD ──────────────────────────────────────────────────────────

def list_coupons() -> list[dict]:
    try:
        res = _client().table("coupons").select("*").order("created_at", desc=True).execute()
        return getattr(res, "data", None) or []
    except Exception as e:
        print(f"[coupons] list failed: {e}")
        return []


def upsert_coupon(data: dict) -> tuple[dict | None, str | None]:
    code = (data.get("code") or "").upper().strip()
    if not code or not code.isascii():
        return None, "Coupon code required (letters/numbers)"
    dtype = (data.get("discount_type") or "percent").lower()
    if dtype not in ("percent", "flat"):
        return None, "discount_type must be 'percent' or 'flat'"
    try:
        dvalue = int(data.get("discount_value"))
    except (TypeError, ValueError):
        return None, "discount_value must be a number"
    if dtype == "percent" and not (1 <= dvalue <= 100):
        return None, "Percent discount must be 1–100"
    if dtype == "flat" and dvalue < 1:
        return None, "Flat discount (in cents) must be positive"

    row = {
        "code": code,
        "description": (data.get("description") or "").strip() or None,
        "discount_type": dtype,
        "discount_value": dvalue,
        "active": bool(data.get("active", True)),
        "auto_apply": bool(data.get("auto_apply", False)),
        "applies_to": data.get("applies_to") or None,
        "max_uses": (int(data["max_uses"]) if data.get("max_uses") not in (None, "") else None),
        "expires_at": data.get("expires_at") or None,
    }
    try:
        res = _client().table("coupons").upsert(row).execute()
        rows = getattr(res, "data", None) or []
        return (rows[0] if rows else row), None
    except Exception as e:
        return None, f"Failed to save coupon: {e}"


def delete_coupon(code: str) -> tuple[bool, str | None]:
    code = (code or "").upper().strip()
    if not code:
        return False, "Coupon code required"
    try:
        _client().table("coupons").delete().eq("code", code).execute()
        return True, None
    except Exception as e:
        return False, f"Failed to delete coupon: {e}"
