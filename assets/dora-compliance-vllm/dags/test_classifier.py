"""
Unit tests for the DORA / BaFin Article 18 classifier (common.classify_incident) and the
deterministic hashing embedding. Run: `pytest dags/test_classifier.py`.

These assert the reference DORA-Pipeline threshold boundaries so the classification stays
faithful if the code is refactored.
"""
from common import (CRITICAL_CLIENT_PCT, CRITICAL_FINANCIAL_EUR, MAJOR_CLIENT_PCT,
                    MAJOR_FINANCIAL_EUR, classify_incident, hash_embed)


def sev(**kw):
    base = {"clients_affected_pct": 0.0, "financial_impact_eur": 0.0,
            "incident_type": "system_outage", "is_cross_border": False,
            "ict_third_party_provider": None}
    base.update(kw)
    return classify_incident(base)["dora_severity"]


def test_critical_by_client_pct():
    assert sev(clients_affected_pct=CRITICAL_CLIENT_PCT) == "critical"
    assert sev(clients_affected_pct=CRITICAL_CLIENT_PCT - 0.1) in ("major", "minor")


def test_critical_by_financial():
    assert sev(financial_impact_eur=CRITICAL_FINANCIAL_EUR) == "critical"
    assert sev(financial_impact_eur=CRITICAL_FINANCIAL_EUR - 1) == "major"


def test_critical_by_cyber():
    # cyber attack + >=10% clients -> critical even below the 25% general threshold
    assert sev(incident_type="cyber_attack", clients_affected_pct=10.0) == "critical"
    assert sev(incident_type="cyber_attack", clients_affected_pct=9.9) == "minor"


def test_critical_by_cross_border():
    assert sev(is_cross_border=True, clients_affected_pct=10.0) == "critical"


def test_major_by_client_pct():
    assert sev(clients_affected_pct=MAJOR_CLIENT_PCT) == "major"
    assert sev(clients_affected_pct=MAJOR_CLIENT_PCT - 0.1) == "minor"


def test_major_by_financial():
    assert sev(financial_impact_eur=MAJOR_FINANCIAL_EUR) == "major"


def test_major_by_third_party_outage():
    assert sev(incident_type="system_outage", ict_third_party_provider="AWS") == "major"
    # third-party set but not a system outage -> not major on that rule alone
    assert sev(incident_type="data_breach", ict_third_party_provider="AWS") == "minor"


def test_minor_default():
    r = classify_incident({"clients_affected_pct": 1.0, "financial_impact_eur": 500.0,
                           "incident_type": "authentication_failure",
                           "is_cross_border": False, "ict_third_party_provider": None})
    assert r["dora_severity"] == "minor"
    assert r["bafin_notification_required"] is False
    assert r["deadline_hours"] is None


def test_deadlines():
    assert classify_incident({"clients_affected_pct": 30.0})["deadline_hours"] == 4
    assert classify_incident({"clients_affected_pct": 12.0})["deadline_hours"] == 72


def test_hash_embed_stable_and_normalised():
    a = hash_embed("cyber attack on payments gateway")
    b = hash_embed("cyber attack on payments gateway")
    assert a == b                          # deterministic
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9   # L2-normalised
    assert len(a) == 256
