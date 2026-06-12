"""Behaviour of the mock enterprise systems (the adapter contracts)."""

from talentflow.domain.models import BGVStatus
from talentflow.integrations.store import EnterpriseStore, reset_store
from talentflow.tools import extend_offer


def make_store() -> EnterpriseStore:
    return EnterpriseStore(seed=7)


def test_budget_check_paths():
    store = make_store()
    ok, detail = store.check_budget("Engineering", 1)
    assert ok and "open slot" in detail
    blocked, detail = store.check_budget("Design", 1)
    assert not blocked
    missing, detail = store.check_budget("Astrology", 1)
    assert not missing and "not found" in detail


def test_comp_band_lookup():
    store = make_store()
    band = store.get_comp_band("l5")
    assert band is not None and band.base_min < band.base_mid < band.base_max
    assert store.get_comp_band("L9") is None


def test_calendar_double_booking_rejected():
    store = make_store()
    slot = store.get_panel_availability()["Asha Menon (EM)"][0]
    first = store.book_interview("cand-priya", "System Design", "Asha Menon (EM)", slot)
    assert first.startswith("Booked")
    second = store.book_interview("cand-marcus", "System Design", "Asha Menon (EM)", slot)
    assert second.startswith("CONFLICT")
    assert len(store.bookings) == 1


def test_candidate_simulator_answers_logistics():
    store = make_store()
    assert "notice period" in store.ask_candidate("cand-priya", "What's your notice period?")
    assert "$192,000" in store.ask_candidate("cand-priya", "What salary do you expect?")
    assert "ERROR" in store.ask_candidate("cand-nobody", "hi?")


def test_offer_negotiation_simulator_converges():
    store = make_store()
    # Priya expects 192k: a low first offer draws a counter...
    reply1 = store.candidate_offer_response("cand-priya", 170_000)
    assert reply1.startswith("COUNTER")
    reply2 = store.candidate_offer_response("cand-priya", 180_000)
    assert reply2.startswith("COUNTER")
    # ...and by round three a near-target number closes.
    reply3 = store.candidate_offer_response("cand-priya", 185_000)
    assert reply3.startswith("ACCEPT")


def test_offer_negotiation_simulator_declines_lowballs():
    store = make_store()
    for _ in range(2):
        store.candidate_offer_response("cand-priya", 150_000)
    assert store.candidate_offer_response("cand-priya", 150_000).startswith("DECLINE")


def test_extend_offer_tool_enforces_ceiling():
    reset_store(seed=7)
    blocked = extend_offer.invoke({"candidate_id": "cand-priya", "base": 200_000, "approved_max": 185_000})
    assert "BLOCKED BY POLICY" in blocked
    allowed = extend_offer.invoke({"candidate_id": "cand-priya", "base": 185_000, "approved_max": 185_000})
    assert "BLOCKED" not in allowed


def test_bgv_vendor_flags_seeded_discrepancy():
    store = make_store()
    flagged_case = store.initiate_bgv("cand-priya")  # seeded with a date discrepancy
    status, detail = store.get_bgv_status(flagged_case)
    assert status == BGVStatus.NEEDS_REVIEW and "differ" in detail

    clean_case = store.initiate_bgv("cand-marcus")
    status, _ = store.get_bgv_status(clean_case)
    assert status == BGVStatus.CLEAR
