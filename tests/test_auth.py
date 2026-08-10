"""Effective-plan expiry + effective-limits (base + top-up) math."""
import auth


# ── plan expiry (expired paid plan reverts to free) ──────────────────
def test_active_plan_kept():
    assert auth.get_effective_plan({"plan": "pro", "plan_expires_at": "2999-01-01T00:00:00Z"}) == "pro"


def test_expired_plan_reverts_to_free():
    assert auth.get_effective_plan({"plan": "pro", "plan_expires_at": "2000-01-01T00:00:00Z"}) == "free"


def test_no_expiry_kept():
    assert auth.get_effective_plan({"plan": "pro", "plan_expires_at": None}) == "pro"


# ── timestamp parsing ────────────────────────────────────────────────
def test_parse_ts_z_suffix():
    assert auth._parse_ts("2030-01-01T00:00:00Z") is not None


def test_parse_ts_garbage_is_none():
    assert auth._parse_ts("not-a-date") is None
    assert auth._parse_ts(None) is None


# ── plan ranking (upgrades must be rank-ups) ─────────────────────────
def test_plan_rank_order():
    assert auth._plan_rank("free") < auth._plan_rank("starter") < auth._plan_rank("pro") < auth._plan_rank("pro_plus")


# ── effective limits: base + top-up additive math ────────────────────
def test_effective_limits_adds_topup(monkeypatch):
    monkeypatch.setattr(auth, "get_plan_limits", lambda plan: {
        "max_chars_per_request": 100, "daily_uses": 1, "monthly_chars": 100,
        "lifetime_uses": None,
    })
    profile = {"plan": "free", "plan_expires_at": None, "user_id": "u1",
               "bonus_uses": 3, "bonus_max_chars_per_request": 500}
    summary = {"topup_credit_30d": 1500, "gen_chars_30d": 20, "uses_24h": 0}
    eff = auth.get_effective_limits(profile, summary)
    assert eff["max_chars_per_request"] == 600      # 100 + 500 top-up
    assert eff["daily_cap"] == 4                     # 1 + 3 bonus gens
    assert eff["monthly_cap"] == 1600               # 100 + 1500 credit
    assert eff["has_topup"] is True


def test_effective_limits_remaining_never_negative(monkeypatch):
    monkeypatch.setattr(auth, "get_plan_limits", lambda plan: {
        "max_chars_per_request": 100, "daily_uses": 1, "monthly_chars": 100,
        "lifetime_uses": None,
    })
    profile = {"plan": "free", "plan_expires_at": None, "user_id": "u1",
               "bonus_uses": 0, "bonus_max_chars_per_request": 0}
    summary = {"topup_credit_30d": 0, "gen_chars_30d": 9999, "uses_24h": 50}
    eff = auth.get_effective_limits(profile, summary)
    assert eff["chars_remaining"] == 0 and eff["daily_remaining"] == 0


def test_effective_limits_admin_unlimited(monkeypatch):
    # Admin base has null caps → effective caps stay None (unlimited).
    monkeypatch.setattr(auth, "get_plan_limits", lambda plan: {
        "max_chars_per_request": None, "daily_uses": None, "monthly_chars": None,
        "lifetime_uses": None,
    })
    profile = {"plan": "admin", "plan_expires_at": None, "user_id": "a1",
               "bonus_uses": 0, "bonus_max_chars_per_request": None}
    summary = {"topup_credit_30d": 0, "gen_chars_30d": 0, "uses_24h": 0}
    eff = auth.get_effective_limits(profile, summary)
    assert eff["daily_cap"] is None and eff["monthly_cap"] is None
