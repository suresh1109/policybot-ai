"""
PolicyBot — Risk Engine v1.0
═══════════════════════════════════════════════════════════════════════════════
Three silent backend modules that run AFTER review_details is confirmed
and BEFORE the recommendation engine is shown to the user:

  1. FraudDetector     — checks for inconsistencies across user data / ID / medical report
  2. RiskScorer        — calculates 0-100 insurance risk score
  3. PremiumPredictor  — estimates monthly premium range

All results are stored in user_profile. Nothing is displayed to the user;
the flow simply continues to the recommendation engine.
═══════════════════════════════════════════════════════════════════════════════
"""

import re
import logging

log = logging.getLogger("PolicyBot")


# ─────────────────────────────────────────────────────────────────────────────
#  FRAUD DETECTOR
# ─────────────────────────────────────────────────────────────────────────────
class FraudDetector:
    """
    Performs consistency checks across:
      - User-stated profile fields
      - Government ID extracted fields (stored after OCR verification)
      - Medical report extracted fields (if uploaded)

    Returns a dict:
      {
        "fraud_status": "LOW" | "MEDIUM" | "HIGH",
        "fraud_issues": "comma-separated list of issues, or None",
        "verification_status": "Verified" | "Partial" | "Unverified"
      }
    """

    @staticmethod
    def _normalise_name(name: str) -> str:
        """Lowercase, strip extra spaces."""
        return re.sub(r"\s+", " ", (name or "").lower().strip())

    @staticmethod
    def _normalise_gender(g: str) -> str:
        g = (g or "").lower().strip()
        if g in ("m", "male", "man"):  return "male"
        if g in ("f", "female", "woman"): return "female"
        return g

    def run(self, profile: dict) -> dict:
        issues = []
        gov_verified = int(profile.get("gov_id_verified") or 0)

        # ── 1. Name check ─────────────────────────────────────────────────
        user_name   = self._normalise_name(profile.get("name", ""))
        id_name     = self._normalise_name(profile.get("id_name_extracted", ""))
        report_name = self._normalise_name(profile.get("medical_report_patient_name", ""))

        if id_name and user_name and id_name not in user_name and user_name not in id_name:
            # Allow partial match (first name only)
            user_parts = set(user_name.split())
            id_parts   = set(id_name.split())
            if not (user_parts & id_parts):   # zero overlap
                issues.append("Name mismatch between user input and government ID")

        if report_name and user_name:
            r_parts = set(report_name.split())
            u_parts = set(user_name.split())
            if not (r_parts & u_parts):
                issues.append("Name mismatch between user input and medical report")

        # ── 2. Age check ──────────────────────────────────────────────────
        try:
            user_age = int(profile.get("age") or 0)
        except (ValueError, TypeError):
            user_age = 0

        try:
            id_age = int(profile.get("id_age_extracted") or 0)
        except (ValueError, TypeError):
            id_age = 0

        try:
            report_age = int(profile.get("medical_report_patient_age") or 0)
        except (ValueError, TypeError):
            report_age = 0

        if id_age and user_age and abs(id_age - user_age) > 3:
            issues.append(f"Age mismatch: user stated {user_age}, ID shows {id_age}")

        if report_age and user_age and abs(report_age - user_age) > 5:
            issues.append(f"Age mismatch: user stated {user_age}, medical report shows {report_age}")

        # ── 3. Gender check ───────────────────────────────────────────────
        user_gender   = self._normalise_gender(profile.get("gender", ""))
        id_gender     = self._normalise_gender(profile.get("id_gender_extracted", ""))
        report_gender = self._normalise_gender(profile.get("medical_report_gender", ""))

        if id_gender and user_gender and id_gender != user_gender:
            issues.append(f"Gender mismatch: user stated {user_gender}, ID shows {id_gender}")

        if report_gender and user_gender and report_gender != user_gender:
            issues.append(f"Gender mismatch: user stated {user_gender}, medical report shows {report_gender}")

        # ── 4. Gov ID verification status ────────────────────────────────
        if not gov_verified:
            issues.append("Government ID not verified — identity unconfirmed")

        # ── 5. Suspicious conditions claimed ────────────────────────────
        # Flag if user claims NO conditions but medical report shows conditions
        user_cond   = (profile.get("medical_conditions") or "").lower()
        report_cond = (profile.get("medical_report_conditions") or "").lower()
        if ("none" in user_cond or not user_cond) and report_cond and "none" not in report_cond:
            issues.append("Medical report indicates conditions not declared by user")

        # ── Assign fraud risk level ───────────────────────────────────────
        n = len(issues)
        if n == 0:
            status = "LOW"
        elif n <= 2:
            status = "MEDIUM"
        else:
            status = "HIGH"

        # Downgrade: if only issue is unverified ID → keep at LOW unless other issues
        if issues == ["Government ID not verified — identity unconfirmed"]:
            status = "LOW"

        verification_status = (
            "Verified"   if gov_verified and n == 0 else
            "Partial"    if gov_verified else
            "Unverified"
        )

        log.info(f"[FRAUD] status={status} issues={issues}")
        return {
            "fraud_status":        status,
            "fraud_issues":        "; ".join(issues) if issues else None,
            "verification_status": verification_status,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  RISK SCORER
# ─────────────────────────────────────────────────────────────────────────────
class RiskScorer:
    """
    Calculates a risk score 0–100 for insurance underwriting.

    Inputs from profile:
      age, medical_conditions, family_medical_conditions,
      family_member_count, coverage_type, fraud_status,
      medical_report_summary, insurance_type

    Output:
      { "risk_score": int, "risk_category": "Low Risk" | "Moderate Risk" | "High Risk" }
    """

    # High-severity conditions (add more points)
    _HIGH_SEV = {"cancer", "heart", "cardiac", "kidney", "renal", "stroke",
                 "hiv", "aids", "liver", "cirrhosis", "parkinson", "alzheimer"}
    # Moderate-severity conditions
    _MOD_SEV  = {"diabetes", "blood pressure", "hypertension", "asthma",
                 "thyroid", "cholesterol", "epilepsy", "arthritis"}

    def run(self, profile: dict) -> dict:
        score = 0

        # ── Age factor (0–30 pts) ─────────────────────────────────────────
        try:
            age = int(profile.get("age") or 0)
        except (ValueError, TypeError):
            age = 0

        if age < 25:   score += 5
        elif age < 35: score += 10
        elif age < 45: score += 15
        elif age < 55: score += 20
        elif age < 65: score += 25
        else:          score += 30

        # ── Main user medical conditions (0–30 pts) ───────────────────────
        cond_raw = (profile.get("medical_conditions") or "").lower()
        if cond_raw and "none" not in cond_raw:
            cond_tokens = {t.strip() for t in re.split(r"[,/]", cond_raw)}
            for token in cond_tokens:
                if any(h in token for h in self._HIGH_SEV):
                    score += 12
                elif any(m in token for m in self._MOD_SEV):
                    score += 7
                else:
                    score += 4

        # ── Family medical conditions (0–15 pts) ─────────────────────────
        fam_cond = (profile.get("family_medical_conditions") or "").lower()
        if fam_cond and "none" not in fam_cond and "healthy" not in fam_cond:
            score += 8
        elif fam_cond and "none" not in fam_cond:
            score += 4

        # ── Family / coverage size (0–10 pts) ────────────────────────────
        try:
            fam_count = int(profile.get("family_member_count") or 0)
        except (ValueError, TypeError):
            fam_count = 0
        score += min(fam_count * 2, 10)

        cov = (profile.get("coverage_type") or "").lower()
        if "whole" in cov or "family" in cov:
            score += 5
        elif "spouse" in cov or "children" in cov:
            score += 3

        # ── Medical report findings (0–10 pts) ───────────────────────────
        report_summary = (profile.get("medical_report_summary") or "").lower()
        if report_summary:
            if any(w in report_summary for w in ["abnormal", "high risk", "critical", "positive"]):
                score += 10
            elif any(w in report_summary for w in ["moderate", "borderline", "elevated"]):
                score += 5
            elif any(w in report_summary for w in ["normal", "healthy", "clear"]):
                score += 0   # no addition for clean report

        # ── Fraud penalty (0–5 pts) ───────────────────────────────────────
        fraud = (profile.get("fraud_status") or "LOW").upper()
        if fraud == "HIGH":    score += 5
        elif fraud == "MEDIUM": score += 2

        # ── Cap at 100 ────────────────────────────────────────────────────
        score = min(score, 100)

        if score <= 30:
            category = "Low Risk"
        elif score <= 60:
            category = "Moderate Risk"
        else:
            category = "High Risk"

        log.info(f"[RISK] score={score} category={category}")
        return {"risk_score": score, "risk_category": category}


# ─────────────────────────────────────────────────────────────────────────────
#  PREMIUM PREDICTOR
# ─────────────────────────────────────────────────────────────────────────────
class PremiumPredictor:
    """
    Estimates a monthly premium range (₹) based on:
      age, risk_score, coverage_type, family_member_count,
      medical_conditions, location (tier), insurance_type

    Output:
      { "premium_prediction": "₹1,800 – ₹2,400 per month" }
    """

    # City tier adjustment multipliers
    _METRO = {"mumbai", "delhi", "bangalore", "bengaluru", "chennai",
              "hyderabad", "kolkata", "pune", "ahmedabad", "surat"}
    _TIER2 = {"jaipur", "lucknow", "kanpur", "nagpur", "indore", "bhopal",
              "patna", "vadodara", "coimbatore", "kochi", "visakhapatnam",
              "agra", "madurai", "nashik", "vijayawada", "thiruvananthapuram"}

    def _city_multiplier(self, city: str) -> float:
        city_l = (city or "").lower().strip()
        if city_l in self._METRO:   return 1.20
        if city_l in self._TIER2:  return 1.05
        return 1.00

    def run(self, profile: dict) -> dict:
        ins_type = (profile.get("insurance_type") or "health").lower()

        try:
            age = int(profile.get("age") or 30)
        except (ValueError, TypeError):
            age = 30
        try:
            risk = int(profile.get("risk_score") or 20)
        except (ValueError, TypeError):
            risk = 20
        try:
            fam_count = int(profile.get("family_member_count") or 0)
        except (ValueError, TypeError):
            fam_count = 0

        city_mult = self._city_multiplier(profile.get("city", ""))
        cov = (profile.get("coverage_type") or "myself").lower()
        has_cond = bool(
            (profile.get("medical_conditions") or "").strip() and
            "none" not in (profile.get("medical_conditions") or "").lower()
        )

        # ── Base premium by insurance type ────────────────────────────────
        if "health" in ins_type:
            base_low, base_high = 600, 900
        elif "term" in ins_type or "life" in ins_type:
            base_low, base_high = 800, 1200
        elif "vehicle" in ins_type:
            base_low, base_high = 500, 800
        elif "travel" in ins_type:
            base_low, base_high = 300, 600
        elif "property" in ins_type:
            base_low, base_high = 400, 700
        elif "accident" in ins_type:
            base_low, base_high = 200, 400
        else:
            base_low, base_high = 600, 900

        # ── Age loading ────────────────────────────────────────────────────
        if age < 30:    age_mult = 1.0
        elif age < 40:  age_mult = 1.15
        elif age < 50:  age_mult = 1.35
        elif age < 60:  age_mult = 1.60
        else:           age_mult = 2.00

        # ── Risk loading ───────────────────────────────────────────────────
        risk_add_pct = risk * 0.5   # 0–50% loading based on risk score

        # ── Condition loading ──────────────────────────────────────────────
        cond_mult = 1.30 if has_cond else 1.00

        # ── Family size loading ────────────────────────────────────────────
        if "whole" in cov or "family" in cov:
            family_mult = 1.0 + (fam_count * 0.15)
        elif "spouse" in cov or "children" in cov:
            family_mult = 1.0 + (fam_count * 0.10)
        else:
            family_mult = 1.0

        # ── Compute final range ───────────────────────────────────────────
        factor = age_mult * (1 + risk_add_pct / 100) * cond_mult * family_mult * city_mult
        low  = int(base_low  * factor / 100) * 100          # round to nearest ₹100
        high = int(base_high * factor / 100) * 100 + 100    # always ensure high > low

        # Format Indian number style
        def _fmt(n):
            if n >= 100000:
                return f"₹{n//100000}.{(n%100000)//10000}L"
            elif n >= 1000:
                return f"₹{n:,}"
            return f"₹{n}"

        prediction = f"{_fmt(low)} – {_fmt(high)} per month"
        log.info(f"[PREMIUM] prediction={prediction} (age={age}, risk={risk}, fam={fam_count})")
        return {"premium_prediction": prediction}


# ─────────────────────────────────────────────────────────────────────────────
#  CONVENIENCE RUNNER — call this once after review_confirmed
# ─────────────────────────────────────────────────────────────────────────────
def run_risk_pipeline(profile: dict, db, user_id: str) -> dict:
    """
    Run fraud → risk → premium in sequence.
    Updates the user profile in DB and returns the merged result dict.
    """
    result = {}

    fraud_result   = FraudDetector().run(profile)
    result.update(fraud_result)
    profile.update(fraud_result)

    risk_result    = RiskScorer().run(profile)
    result.update(risk_result)
    profile.update(risk_result)

    premium_result = PremiumPredictor().run(profile)
    result.update(premium_result)
    profile.update(premium_result)

    # ── Add claim probability using fraud_risk fn ──────────────────────────
    try:
        from models.fraud_risk import _predict_claim_probability
        age_raw = str(profile.get("age") or "")
        import re as _re
        _m = _re.search(r"\b(\d{1,3})\b", age_raw)
        _age = int(_m.group(1)) if _m else None
        claim_prob = _predict_claim_probability(profile, result.get("risk_score", 0), _age)
        result["claim_probability"] = claim_prob
        profile["claim_probability"] = claim_prob
    except Exception:
        claim_prob = 0

    # Persist to DB — only fields that exist as columns (upsert is column-safe)
    db.upsert_user_profile(user_id, {
        "fraud_status":       result.get("fraud_status"),
        "fraud_issues":       result.get("fraud_issues"),
        "risk_score":         result.get("risk_score"),
        "risk_category":      result.get("risk_category"),
        "premium_prediction": result.get("premium_prediction"),
        "claim_probability":  result.get("claim_probability", 0),
    })

    log.info(f"[RISK-PIPELINE] user={user_id} "
             f"fraud={result.get('fraud_status')} "
             f"risk={result.get('risk_score')} "
             f"premium={result.get('premium_prediction')}")
    return result
