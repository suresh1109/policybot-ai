"""
fraud_risk.py — Fraud Detection, Risk Scoring, and Premium Prediction Engine
PolicyBot v11 — Runs silently after user confirms review_details.
No visible output to user. Stores results in user profile for recommendation engine.
"""
import re, json, logging

log = logging.getLogger("PolicyBot")

# ─────────────────────────────────────────────────────────────────────────────
# FRAUD DETECTION
# Compares user-provided data vs Gov-ID extracted data vs medical report data
# ─────────────────────────────────────────────────────────────────────────────

def run_fraud_detection(profile: dict) -> dict:
    """
    Check for inconsistencies between user-provided data, ID-extracted data,
    and medical report data. Returns fraud_status (LOW/MEDIUM/HIGH) + issues list.
    """
    issues = []

    # ── Helper: normalize strings for comparison ──────────────────────────────
    def _norm(s):
        return re.sub(r"\s+", " ", str(s or "")).strip().lower()

    def _extract_age_int(val):
        """Extract integer age from string like '35', '35 years', etc."""
        if not val:
            return None
        m = re.search(r"\b(\d{1,3})\b", str(val))
        return int(m.group(1)) if m else None

    # ── 1. Name check: profile name vs medical report name ────────────────────
    user_name   = _norm(profile.get("name", ""))
    report_name = _norm(profile.get("medical_report_patient_name", ""))
    if user_name and report_name:
        # Fuzzy: at least one word should overlap
        user_words   = set(user_name.split())
        report_words = set(report_name.split())
        overlap      = user_words & report_words
        if len(user_name) > 3 and len(report_name) > 3 and not overlap:
            issues.append(f"Name mismatch: profile name '{profile.get('name','')}' "
                          f"vs medical report '{profile.get('medical_report_patient_name','')}'.")

    # ── 2. Age check: profile age vs ID extracted age (stored in doc_type_found era)
    #    The OCR pipeline stores profile.age from ID. Compare with medical report age if any.
    user_age   = _extract_age_int(profile.get("age"))
    report_age = _extract_age_int(profile.get("medical_report_patient_age", ""))
    if user_age and report_age:
        diff = abs(user_age - report_age)
        if diff > 5:
            issues.append(f"Age mismatch: user provided {user_age} years but "
                          f"medical report shows {report_age} years (diff={diff}).")
        elif diff > 2:
            issues.append(f"Minor age discrepancy ({diff} years) between profile "
                          f"and medical report — please verify.")

    # ── 3. Gender check: profile gender vs medical report gender ─────────────
    user_gender   = _norm(profile.get("gender", ""))
    report_gender = _norm(profile.get("medical_report_patient_gender", ""))
    if user_gender and report_gender:
        if user_gender not in ("unknown", "") and report_gender not in ("unknown", ""):
            if user_gender != report_gender:
                issues.append(f"Gender mismatch: profile shows '{profile.get('gender','')}' "
                               f"but medical report shows '{profile.get('medical_report_patient_gender','')}'.")

    # ── 4. Medical condition cross-check ─────────────────────────────────────
    declared_cond  = _norm(profile.get("medical_conditions", "") or "")
    report_cond    = _norm(profile.get("medical_report_conditions", "") or "")
    cond_status    = _norm(profile.get("medical_conditions_status", "") or "")

    # If user said "no conditions" but report shows conditions
    if cond_status == "none" and report_cond and report_cond not in ("", "none", "normal", "clear"):
        issues.append(f"Possible condition concealment: user declared no medical conditions "
                      f"but health report indicates: '{profile.get('medical_report_conditions','')}'.")

    # ── 5. Gov ID verification check ─────────────────────────────────────────
    gov_verified = int(profile.get("gov_id_verified", 0) or 0)
    if gov_verified == 0:
        issues.append("Government ID was not verified or was skipped.")

    # ── 6. Determine fraud risk level ─────────────────────────────────────────
    critical_count = sum(1 for i in issues if "mismatch" in i.lower() or "concealment" in i.lower())
    minor_count    = len(issues) - critical_count

    if critical_count >= 2:
        level = "HIGH"
    elif critical_count == 1:
        level = "MEDIUM"
    elif minor_count >= 2:
        level = "MEDIUM"
    else:
        level = "LOW"

    result = {
        "fraud_status":  level,
        "fraud_issues":  json.dumps(issues) if issues else "[]",
    }
    log.info(f"[FRAUD] level={level} issues_count={len(issues)}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# RISK SCORING
# 0–30 = Low, 31–60 = Moderate, 61–100 = High
# ─────────────────────────────────────────────────────────────────────────────

# Medical condition severity weights
_CONDITION_SEVERITY = {
    # Critical — high risk (+20)
    "cancer":20, "tumour":20, "tumor":20, "hiv":20, "aids":20,
    "chronic kidney":20, "renal failure":20, "heart failure":20,
    "stroke":18, "paralysis":18, "alzheimer":18,
    # Serious (+12)
    "diabetes":12, "heart disease":12, "coronary":12, "cardiac":12,
    "hypertension":10, "blood pressure":8, "thyroid":8,
    "asthma":8, "copd":8, "epilepsy":10,
    # Moderate (+5)
    "obesity":5, "depression":5, "anxiety":4, "arthritis":4,
    "migraine":3, "pcod":4, "pcos":4,
    # Minor (+2)
    "allergy":2, "vision":2, "dental":1,
}

def _condition_score(conditions_text: str) -> int:
    """Return risk points from a conditions string."""
    if not conditions_text:
        return 0
    text = conditions_text.lower()
    pts  = 0
    for kw, wt in _CONDITION_SEVERITY.items():
        if kw in text:
            pts += wt
    return min(pts, 35)  # cap at 35 pts from conditions alone

def run_risk_scoring(profile: dict, fraud_result: dict) -> dict:
    """
    Calculate a 0–100 risk score using:
    - Age factor
    - Medical condition severity
    - Family coverage size
    - Medical report findings
    - Fraud level penalty
    Returns risk_score (int) + risk_category (str) + premium_prediction (str)
    """
    score = 0

    # ── Age factor (0–20 pts) ─────────────────────────────────────────────────
    age = None
    try:
        age_raw = str(profile.get("age") or "")
        m = re.search(r"\b(\d{1,3})\b", age_raw)
        if m:
            age = int(m.group(1))
    except Exception:
        pass

    if age:
        if age < 25:
            age_pts = 5
        elif age < 35:
            age_pts = 8
        elif age < 45:
            age_pts = 12
        elif age < 55:
            age_pts = 16
        elif age < 65:
            age_pts = 18
        else:
            age_pts = 20
        score += age_pts
    else:
        score += 10  # unknown age → moderate

    # ── Medical conditions (0–35 pts) ─────────────────────────────────────────
    declared  = profile.get("medical_conditions", "") or ""
    report_c  = profile.get("medical_report_conditions", "") or ""
    family_c  = profile.get("family_medical_conditions", "") or ""

    cond_score = max(
        _condition_score(declared),
        _condition_score(report_c),
    )
    score += cond_score

    # Family conditions add half weight
    fam_score = _condition_score(family_c) // 2
    score += min(fam_score, 10)

    # ── Family coverage size (0–10 pts) ───────────────────────────────────────
    try:
        fam_count = int(profile.get("family_member_count") or 0)
    except Exception:
        fam_count = 0

    coverage = (profile.get("coverage_type") or "").lower()
    if "whole family" in coverage or "full family" in coverage:
        score += min(fam_count * 2, 10)
    elif "spouse" in coverage or "children" in coverage:
        score += min(fam_count * 1, 6)

    # ── Medical report uploaded & findings ────────────────────────────────────
    report_uploaded = int(profile.get("medical_report_uploaded") or
                          profile.get("condition_report_uploaded") or 0)
    report_summary  = (profile.get("medical_report_summary") or "").lower()
    if report_uploaded and report_summary:
        # If report shows abnormal findings, add points
        abnormal_kw = ["abnormal", "elevated", "high", "positive", "borderline",
                       "risk", "concern", "irregular", "deficiency"]
        if any(kw in report_summary for kw in abnormal_kw):
            score += 10
        else:
            score -= 3  # Clean report slightly reduces risk

    # ── Fraud penalty ──────────────────────────────────────────────────────────
    fraud_lvl = (fraud_result.get("fraud_status") or "LOW").upper()
    if fraud_lvl == "HIGH":
        score += 15
    elif fraud_lvl == "MEDIUM":
        score += 7

    # ── Cap score ─────────────────────────────────────────────────────────────
    score = max(0, min(100, score))

    # ── Category ──────────────────────────────────────────────────────────────
    if score <= 30:
        category = "Low Risk"
    elif score <= 60:
        category = "Moderate Risk"
    else:
        category = "High Risk"

    # ── Premium Prediction ────────────────────────────────────────────────────
    premium_range = _predict_premium(profile, score, age, fam_count)

    log.info(f"[RISK] score={score} category={category} premium={premium_range}")

    # ── Claim Probability Score (0–100%) ─────────────────────────────────────
    # Derived from risk score + insurance type + age + conditions
    claim_prob = _predict_claim_probability(profile, score, age)

    return {
        "risk_score":           score,
        "risk_category":        category,
        "premium_prediction":   premium_range,
        "claim_probability":    claim_prob,   # int 0–100
    }


# ─────────────────────────────────────────────────────────────────────────────
# PREMIUM PREDICTION
# ─────────────────────────────────────────────────────────────────────────────

def _predict_premium(profile: dict, risk_score: int, age: int | None, fam_count: int) -> str:
    """
    Estimate monthly premium range in INR.
    Base rates by insurance type, adjusted for age, risk score, coverage size.
    """
    ins_type = (profile.get("insurance_type") or "health insurance").lower()

    # ── Base monthly premium by type (single person, age 30, no conditions) ──
    if "health" in ins_type:
        base_low, base_high = 500, 1000
    elif "term" in ins_type or "life" in ins_type:
        base_low, base_high = 800, 1500
    elif "vehicle" in ins_type or "motor" in ins_type or "car" in ins_type:
        base_low, base_high = 1000, 2000
    elif "travel" in ins_type:
        base_low, base_high = 300, 700
    elif "property" in ins_type or "home" in ins_type:
        base_low, base_high = 600, 1200
    elif "accident" in ins_type:
        base_low, base_high = 200, 500
    else:
        base_low, base_high = 500, 1200

    # ── Age multiplier ────────────────────────────────────────────────────────
    if age:
        if age < 25:    age_mult = 0.8
        elif age < 35:  age_mult = 1.0
        elif age < 45:  age_mult = 1.3
        elif age < 55:  age_mult = 1.7
        elif age < 65:  age_mult = 2.2
        else:           age_mult = 2.8
    else:
        age_mult = 1.2

    # ── Risk score multiplier ─────────────────────────────────────────────────
    if risk_score <= 20:
        risk_mult = 1.0
    elif risk_score <= 40:
        risk_mult = 1.3
    elif risk_score <= 60:
        risk_mult = 1.7
    else:
        risk_mult = 2.2

    # ── Family size multiplier ────────────────────────────────────────────────
    coverage = (profile.get("coverage_type") or "").lower()
    if "whole family" in coverage or "full family" in coverage:
        fam_mult = 1.0 + (fam_count * 0.35)
    elif "spouse" in coverage or "children" in coverage:
        fam_mult = 1.0 + (fam_count * 0.2)
    else:
        fam_mult = 1.0

    # ── Calculate range ───────────────────────────────────────────────────────
    total_mult = age_mult * risk_mult * fam_mult
    low  = int(base_low  * total_mult / 100) * 100   # round to nearest 100
    high = int(base_high * total_mult / 100) * 100

    # Ensure minimum gap
    if high - low < 400:
        high = low + 400

    return f"₹{low:,} – ₹{high:,} per month"


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE REVIEW SUMMARY BUILDER
# Returns a formatted markdown-style summary string for the review_details stage
# ─────────────────────────────────────────────────────────────────────────────

def build_review_summary(profile: dict) -> str:
    """Build a human-readable profile summary for the review_details stage."""

    def _val(key, fallback="Not provided"):
        v = profile.get(key)
        return str(v).strip() if v and str(v).strip() else fallback

    lines = []
    lines.append("📋 **Your Insurance Profile Summary**\n")

    # Personal details
    lines.append(f"👤 **Name:** {_val('name')}")
    lines.append(f"🎂 **Age:** {_val('age')} years" if profile.get('age') else f"🎂 **Age:** Not provided")
    lines.append(f"⚧ **Gender:** {_val('gender')}")
    lines.append(f"🏙️ **City:** {_val('city')}")

    # Insurance details
    lines.append(f"\n🛡️ **Insurance Type:** {_val('insurance_type')}")
    lines.append(f"👨‍👩‍👧 **Coverage Type:** {_val('coverage_type')}")

    # Family members
    fam_count = profile.get("family_member_count")
    fam_json  = profile.get("family_members_json")
    coverage  = (profile.get("coverage_type") or "").lower()

    if fam_count and int(fam_count or 0) > 0 and "myself" not in coverage:
        lines.append(f"\n👨‍👩‍👧‍👦 **Family Members:** {fam_count} member(s)")
        if fam_json:
            try:
                members = json.loads(fam_json)
                for m in members:
                    rel = m.get("relationship", "Member")
                    age = m.get("age", "?")
                    lines.append(f"   • {rel} — Age {age}")
            except Exception:
                pass
        fam_cond = profile.get("family_medical_conditions", "")
        if fam_cond and fam_cond.lower() not in ("", "none"):
            lines.append(f"   🏥 Family health notes: {fam_cond}")
        else:
            lines.append(f"   ✅ Family health: No known conditions")

    # Health information
    med_status = (profile.get("medical_conditions_status") or "").lower()
    med_cond   = profile.get("medical_conditions") or ""
    if med_cond and med_cond.lower() not in ("", "none"):
        lines.append(f"\n🏥 **Medical Conditions:** {med_cond}")
    else:
        lines.append(f"\n✅ **Medical Conditions:** None declared")

    # Medical report
    report_uploaded = int(profile.get("medical_report_uploaded") or
                          profile.get("condition_report_uploaded") or 0)
    report_summary  = profile.get("medical_report_summary", "")
    if report_uploaded:
        if report_summary:
            lines.append(f"📄 **Medical Report:** Uploaded — {report_summary[:100]}")
        else:
            lines.append(f"📄 **Medical Report:** Uploaded ✅")

    # Budget
    budget = profile.get("budget_range", "")
    if budget:
        lines.append(f"\n💰 **Preferred Budget:** {budget}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# RISK DISPLAY HELPER (used by _build_prompt for recommendation)
# ─────────────────────────────────────────────────────────────────────────────

def format_risk_context(profile: dict) -> str:
    """Short risk summary for injection into recommendation prompt."""
    risk  = profile.get("risk_score") or 0
    cat   = profile.get("risk_category") or "Unknown"
    pred  = profile.get("premium_prediction") or ""
    fraud = profile.get("fraud_status") or "PENDING"

    parts = [f"Risk Score: {risk}/100 ({cat})"]
    if pred:
        parts.append(f"Estimated Premium: {pred}")
    if fraud not in ("LOW", "PENDING", ""):
        parts.append(f"Fraud Flag: {fraud}")
    return " | ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# CLAIM PROBABILITY PREDICTION
# Predicts likelihood (0–100 int) the user will file a claim within 2 years.
# Based on: insurance type base rate + age + medical conditions + risk score
# ─────────────────────────────────────────────────────────────────────────────

def _predict_claim_probability(profile: dict, risk_score: int, age) -> int:
    """
    Returns a 0–100 integer representing the % probability of a claim
    within the first 2 years of the policy.
    """
    ins = (profile.get("insurance_type") or "health").lower()

    # ── Base rate by insurance type ──────────────────────────────────────────
    if "health" in ins:
        base = 35
    elif "vehicle" in ins or "motor" in ins:
        base = 28
    elif "term" in ins or "life" in ins:
        base = 12    # term claims only on death
    elif "travel" in ins:
        base = 20
    elif "property" in ins or "home" in ins:
        base = 10
    elif "accident" in ins:
        base = 18
    else:
        base = 25

    # ── Age adjustment ───────────────────────────────────────────────────────
    age_add = 0
    if age:
        if age < 25:    age_add = -5
        elif age < 35:  age_add =  0
        elif age < 45:  age_add =  8
        elif age < 55:  age_add = 16
        elif age < 65:  age_add = 24
        else:           age_add = 32

    # ── Medical condition penalty ────────────────────────────────────────────
    cond_raw = (
        (profile.get("medical_conditions") or "") + " " +
        (profile.get("medical_report_conditions") or "")
    ).lower()
    cond_add = 0
    severe   = ["cancer", "kidney", "heart", "cardiac", "renal", "dialysis"]
    moderate = ["diabetes", "hypertension", "blood pressure", "asthma", "thyroid", "copd"]
    if any(k in cond_raw for k in severe):
        cond_add = 28
    elif any(k in cond_raw for k in moderate):
        cond_add = 15
    elif cond_raw.strip() and cond_raw.strip() not in ("none", "no", "no conditions"):
        cond_add = 7

    # ── Risk score contribution (scaled 0–15 pts) ────────────────────────────
    risk_add = int(risk_score * 0.15)

    # ── Combine + clamp 1–95 ────────────────────────────────────────────────
    total = base + age_add + cond_add + risk_add
    return max(1, min(95, total))
