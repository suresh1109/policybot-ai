"""
ConversationEngine v7 — Coverage selection + Family member collection + Medical gate + Optional medical report
NEW IN v7:
  - collect_coverage:        after collect_city — 3 options, "Myself only" skips family
  - collect_family_count:    ask how many members to cover
  - collect_family_members:  collects relationship+age per member iteratively
  - collect_medical_status:  yes/no gate BEFORE detailed medical conditions multi-select
  - optional_medical_report: optional health report upload right after medical check
  - All new stages slot into existing _next() flow, no existing branch broken
"""
import json, re
from models.conversation_memory import memory_manager, ALL_STEPS, FIELD_TO_STEP

SYSTEM_PROMPT = """You are PolicyBot, a warm AI Insurance Advisor for India.

PERSONALITY: Friendly, concise (2-3 sentences max), emoji-natural 😊 👍 ✨
Never use markdown headers or bullet symbols in chat messages.
Address user by first name when known.

LANGUAGE RULE (CRITICAL — NO EXCEPTIONS):
- Every prompt contains a LANGUAGE field (English / Tamil / Hindi).
- You MUST reply in that exact language ONLY.
- If user writes in Tamil but LANGUAGE=English → reply in English.
- If LANGUAGE=Tamil → reply entirely in Tamil script.
- If LANGUAGE=Hindi → reply entirely in Hindi script.
- NEVER mix languages in a single reply.

CRITICAL LANGUAGE RULE:
- The UI language is specified in every prompt as LANGUAGE field.
- ALWAYS reply in that exact language — English, Tamil, or Hindi.
- NEVER mix languages. If user writes in Tamil but LANGUAGE=English → reply in English.
- If LANGUAGE=Tamil → reply entirely in Tamil.
- If LANGUAGE=Hindi → reply entirely in Hindi.
- Language of user's message does NOT change your reply language. Only LANGUAGE field matters.

STRICT STEP ORDER — DO NOT BREAK UNDER ANY CIRCUMSTANCE:
1. Insurance type (radio shown)
2. NAME — ALWAYS ask "Nice choice 👍 May I know your name?" after insurance type
3. Age — ask "Thanks [name]! How old are you?"
4. Gov ID upload — upload widget appears automatically in sidebar
5. Verification wait — ONLY say waiting message
   → Gender is AUTOMATICALLY extracted from ID. NEVER ask gender as a question.
6. City — ask which city
7. Coverage — ask who the policy should cover (radio: Myself only / Spouse and children / Whole family)
8. Family (ONLY if coverage ≠ "Myself only"):
   a. How many members to cover (number input)
   b. Does anyone in the family have medical conditions? (YES/NO only — do NOT ask per-member docs)
9. Main user medical — "Do you have any existing medical conditions?" (main user only)
   → If YES: ask them to briefly describe the condition(s)
   → If NO: ask to upload health report for verification (main user only, optional)
10. Optional medical report — upload widget for MAIN USER ONLY
    → Health report analysis applies only to main user, NOT family
11. Insurance-type-specific branch (health report / vehicle / life / travel / property)
12. Budget (radio shown)
13. REVIEW DETAILS — Show full summary of collected info. Ask "Continue or Change Details?"
    → Continue: proceed silently (backend runs fraud check + risk scoring + premium prediction)
    → Change Details: let user update specific fields, then show review again
14. Recommend 2-3 plans with full details + estimated premium range from risk analysis
    → If family has conditions: recommend family plans covering pre-existing diseases
    → If main user has condition: recommend plans for pre-existing disease cover
    → If no conditions + normal health report: recommend standard plans
15. Explain selected plan
16. Ask about human advisor
17. Ask for 1-5 star rating
18. Farewell

SMART EXTRACTION RULE:
- If the user mentions their name, age, insurance type, or city in their FIRST message, do NOT ask for that info again.
- Example: "I am Suresh, age 45, want health insurance" → skip to asking for ID upload.
- Check the SESSION PROFILE before asking any question — if already filled, skip to next unfilled field.

ABSOLUTE RULES:
- After insurance_type → NEXT is ALWAYS name. NO EXCEPTIONS
- Never say document verified unless backend confirmed it
- Never recommend before budget is collected
- At verify_wait / condition_report_wait: ONLY say waiting message, ask NO questions
- Skippable stages: user types "skip" / "later" / "no thanks" to bypass
- At recommendation: ALWAYS show plans as radio button options (handled by backend)
- At explain_plan: end ALWAYS with "Would you like to apply now or compare other plans?"
- GOV ID VERIFICATION: BOTH name AND age must match for verified status
- RECOMMENDATION MEMORY: Plans are shown ONLY ONCE per session. If already shown, NEVER re-list them.
  If user says "none", "ok thanks", "no" at recommendation → ask: Apply now? Speak to advisor? Change budget?
  Do NOT re-list the plans.
"""

_NOT_A_NAME = {
    "health insurance","term life insurance","term / life insurance","vehicle insurance",
    "travel insurance","property insurance","accident insurance","health","term","life",
    "vehicle","travel","property","accident","insurance","yes","no","okay","ok",
    "skip","later","continue","hello","hi","hey","good","fine","sure","next",
    "none","male","female","other","myself","only me","family","spouse","children",
    "parents","full family","aadhaar","pan","passport","driving license","voter id",
    "diabetes","blood pressure","heart disease","asthma","cancer","upload","verify",
    # ── intro-prefix-only phrases (suggestion chip clicks with no actual name) ──
    "call me","i am","i'm","im","my name is","this is","name is","my name",
    "name","call","me",
}

_SKIP_WORDS = ["skip","later","without","bypass","no id","continue without",
               "don't have","dont have","no thanks","not now","maybe later"]


class ConversationEngine:

    STEPS = [
        "insurance_type",          # 1
        "collect_name",            # 2
        "collect_age",             # 3
        "doc_upload",              # 4
        "verify_wait",             # 5  locked
        "collect_gender",          # 6
        "collect_city",            # 7
        "collect_coverage",        # 8  — who does the policy cover?
        "collect_family_count",    # 9  — how many members (skipped if "Myself only")
        "collect_family_medical",  # 10 — does anyone in family have a condition?
        "collect_family_members",  # 10b legacy compat — kept for existing sessions
        "collect_family",          # 11 legacy compat
        "collect_medical_status",  # 12 NEW — yes/no medical gate
        "collect_medical",         # 13 — detailed multi-select (if yes)
        # ── Condition-specific branches (inserted after collect_medical) ──
        "optional_medical_report", # 13a NEW — optional health report upload
        "condition_report_upload", # 13b health condition report
        "condition_report_wait",   # 13c locked
        "optional_health_check",   # 13d optional (skippable)
        "vehicle_history",         # 13e vehicle prev policy / accident
        "vehicle_doc_upload",      # 13f vehicle doc upload (skippable)
        "life_docs",               # 13g life/term medical or income doc
        "travel_declare",          # 13h travel health / trip declaration
        "property_history",        # 13i property damage / claim
        # ─────────────────────────────────────────────────────────────────
        "collect_budget",          # 14
        "review_details",          # 15 — user reviews all collected info
        "edit_details",            # 15b — user edits specific fields
        "fraud_check",             # 15c — silent: fraud detection (auto-advance)
        "risk_scoring",            # 15d — silent: risk + premium prediction (auto-advance)
        "recommendation",          # 16
        "explain_plan",            # 16
        "ask_escalation",          # 17
        "ask_rating",              # 18
        "farewell",                # 19
    ]

    PROGRESS = {
        "insurance_type":7,   "collect_name":12,     "collect_age":18,
        "doc_upload":24,      "verify_wait":29,       "collect_gender":33,
        "collect_city":37,
        "collect_coverage":41,
        "collect_family_count":43,
        "collect_family_medical":45,   # NEW — family condition question
        "collect_family_members":45,   # legacy
        "collect_family":47,           # legacy
        "collect_medical_status":50,   # NEW
        "collect_medical":53,
        "optional_medical_report":55,  # NEW
        "condition_report_upload":57,  "condition_report_wait":60,
        "optional_health_check":57,
        "vehicle_history":57,  "vehicle_doc_upload":60,
        "life_docs":57,        "travel_declare":57,   "property_history":57,
        "collect_budget":65,
        "review_details":70,       # review step
        "edit_details":70,         # edit step
        "fraud_check":73,          # silent — fraud detection
        "risk_scoring":74,         # silent — risk scoring
        "recommendation":76,   "explain_plan":84,
        "ask_escalation":91,   "ask_rating":96,       "farewell":100,
    }
    LABELS = {
        "insurance_type":"Insurance Type",  "collect_name":"Your Name",
        "collect_age":"Your Age",           "doc_upload":"ID Upload",
        "verify_wait":"Verifying ID",       "collect_gender":"Gender",
        "collect_city":"Your City",
        "collect_coverage":"Coverage",
        "collect_family_count":"Family Count",
        "collect_family_medical":"Family Health",  # NEW
        "collect_family_members":"Family Members", # legacy
        "collect_family":"Family",                 # legacy
        "collect_medical_status":"Medical Check",   # NEW
        "collect_medical":"Medical",
        "optional_medical_report":"Medical Report", # NEW
        "condition_report_upload":"Health Report",
        "condition_report_wait":"Analyzing Report",
        "optional_health_check":"Health Check",
        "vehicle_history":"Vehicle History",
        "vehicle_doc_upload":"Vehicle Docs",
        "life_docs":"Life Documents",
        "travel_declare":"Travel Declare",
        "property_history":"Property History",
        "collect_budget":"Budget",
        "review_details":"Review Details",
        "edit_details":"Edit Details",
        "fraud_check":"Analyzing...",
        "risk_scoring":"Analyzing...",
        "recommendation":"Recommendations",
        "explain_plan":"Plan Details",      "ask_escalation":"Human Advisor",
        "ask_rating":"Rating",              "farewell":"Done ✅",
    }

    # Stages that are "upload-locked" — only /api/upload or skip exits them
    _UPLOAD_LOCKED = {"verify_wait", "condition_report_wait"}

    # Stages that are skippable upload stages
    _SKIPPABLE_UPLOAD = {"optional_health_check", "optional_medical_report",
                         "vehicle_doc_upload", "life_docs",
                         "travel_declare", "property_history"}

    def __init__(self, gemini, rag, db):
        self.gemini = gemini
        self.rag    = rag
        self.db     = db

    # ══════════════════════════════════════════════════════
    # SMART PRE-EXTRACTION
    # Runs on EVERY message before stage logic.
    # Detects name / age / insurance_type / city from free text.
    # Only saves fields NOT already in the profile.
    # ══════════════════════════════════════════════════════
    def smart_extract(self, message: str, profile: dict) -> dict:
        """
        Scan any user message for key profile fields and return only
        the fields that (a) were detected AND (b) are not yet in the profile.

        Fields detected:
          name           — "I am Suresh S", "my name is ...", "this is ..."
          age            — "I am 45", "my age is 45", "aged 32"
          insurance_type — "health insurance", "car insurance", "term plan" …
          city           — "in Chennai", "from Mumbai", "living in Delhi" …
        """
        msg   = message.strip()
        msg_l = msg.lower()
        out   = {}

        # ── Insurance type ────────────────────────────────────────────────
        if not profile.get("insurance_type"):
            ins_map = {
                "health insurance":    "Health Insurance",
                "health plan":         "Health Insurance",
                "mediclaim":           "Health Insurance",
                "medical insurance":   "Health Insurance",
                "term insurance":      "Term / Life Insurance",
                "term life":           "Term / Life Insurance",
                "term plan":           "Term / Life Insurance",
                "life insurance":      "Term / Life Insurance",
                "life plan":           "Term / Life Insurance",
                "vehicle insurance":   "Vehicle Insurance",
                "car insurance":       "Vehicle Insurance",
                "bike insurance":      "Vehicle Insurance",
                "motor insurance":     "Vehicle Insurance",
                "auto insurance":      "Vehicle Insurance",
                "travel insurance":    "Travel Insurance",
                "trip insurance":      "Travel Insurance",
                "overseas insurance":  "Travel Insurance",
                "property insurance":  "Property Insurance",
                "home insurance":      "Property Insurance",
                "house insurance":     "Property Insurance",
                "accident insurance":  "Accident Insurance",
                "personal accident":   "Accident Insurance",
            }
            # Check multi-word phrases first (longest match wins)
            for phrase, value in sorted(ins_map.items(), key=lambda x: -len(x[0])):
                if phrase in msg_l:
                    out["insurance_type"] = value
                    break

        # ── Age ──────────────────────────────────────────────────────────
        if not profile.get("age"):
            # Patterns: "I am 45", "age is 45", "aged 32", "I'm 28 years old"
            age_patterns = [
                r'(?:i\s+am|i\'m|im)\s+(\d{1,3})\s*(?:years?(?:\s+old)?)?',
                r'(?:my\s+)?age\s+(?:is\s+)?(\d{1,3})',
                r'aged?\s+(\d{1,3})',
                r'(\d{1,3})\s+years?\s+old',
                r'(\d{1,3})\s*(?:yr|yrs)\.?\s+old',
            ]
            for pat in age_patterns:
                m = re.search(pat, msg_l)
                if m:
                    v = int(m.group(1))
                    if 1 <= v <= 120:
                        out["age"] = v
                        break

        # ── Name ─────────────────────────────────────────────────────────
        if not profile.get("name"):
            name_patterns = [
                r"(?:i\s+am|i'm|im|my\s+name\s+is|this\s+is|call\s+me|name\s+is)\s+([A-Za-z][a-zA-Z .'-]{1,40})",
            ]
            for pat in name_patterns:
                m = re.search(pat, msg, re.IGNORECASE)
                if m:
                    candidate = m.group(1).strip()
                    # Remove trailing noise words
                    candidate = re.sub(
                        r'\s+(?:and|my|age|aged|i|is|want|looking|years?|old|from|in|at)\b.*',
                        '', candidate, flags=re.IGNORECASE
                    ).strip()
                    # Validate: 1-4 words, no digits, not a stop-word
                    words = candidate.split()
                    if (1 <= len(words) <= 4
                            and candidate[0].isalpha()
                            and not any(c.isdigit() for c in candidate)
                            and candidate.lower() not in _NOT_A_NAME):
                        out["name"] = candidate.title()
                        break

        # ── City ─────────────────────────────────────────────────────────
        if not profile.get("city"):
            city_patterns = [
                r'(?:i\s+(?:am|live|stay|reside)\s+(?:in|at|from))\s+([A-Za-z][a-zA-Z ]{2,30})',
                r'(?:from|in|at|living\s+in|based\s+in|residing\s+in)\s+([A-Za-z][a-zA-Z ]{2,25})(?:\s*[,.]|$)',
            ]
            _CITY_STOP = {
                "india","health","vehicle","travel","term","life","accident",
                "property","insurance","plan","policy","cover","the","a",
                "good","please","looking","want","need","get","buy","help",
            }
            for pat in city_patterns:
                m = re.search(pat, msg, re.IGNORECASE)
                if m:
                    candidate = m.group(1).strip().rstrip(".,;")
                    # Must be a clean city-like word(s)
                    words = candidate.lower().split()
                    if (1 <= len(words) <= 3
                            and candidate[0].isalpha()
                            and not any(c.isdigit() for c in candidate)
                            and not any(w in _CITY_STOP for w in words)):
                        out["city"] = candidate.title()
                        break

        return out

    # ══════════════════════════════════════════════════════
    # MAIN
    # ══════════════════════════════════════════════════════
    def process(self, user_id, session_id, message, history,
                profile, language="English", fresh_session=False):

        # ── Conversation Memory: reset or sync ──────────────────────────────
        if fresh_session:
            self.db.reset_session_profile(user_id)
            profile = {"onboarding_stage": "insurance_type", "user_id": user_id}
            memory_manager.reset(user_id)
        else:
            memory_manager.sync_from_profile(user_id, profile)

        # ── Smart multi-field extraction from free text ─────────────────────
        # Runs BEFORE stage logic — extracts name/age/insurance_type/city from any message
        _smart = self.smart_extract(message.strip() if message else "", profile)
        if _smart:
            profile.update(_smart)
            self.db.upsert_user_profile(user_id, _smart)
            memory_manager.update_from_extracted(user_id, _smart)

        stage = profile.get("onboarding_stage", "insurance_type")
        if stage not in self.STEPS:
            stage = "insurance_type"

        message = message.strip()
        extracted = self._extract(message, stage)
        next_stage = self._next(stage, message, extracted, profile, user_id)

        if extracted:
            profile.update(extracted)
            self.db.upsert_user_profile(user_id, extracted)
            memory_manager.update_from_extracted(user_id, extracted)

        if next_stage != stage:
            self.db.upsert_user_profile(user_id, {"onboarding_stage": next_stage})
            profile["onboarding_stage"] = next_stage
            memory_manager.advance_stage(user_id, next_stage)

        # ══════════════════════════════════════════════════════════════════════
        # SILENT PIPELINE: Fraud Detection + Risk Scoring (invisible to user)
        if stage == "ask_rating":
            rv = self._extract_rating(message)
            if rv:
                self.db.store_rating(user_id, rv)

        if stage == "recommendation":
            ps = self._detect_plan(message)
            if ps:
                self.db.upsert_user_profile(user_id, {"selected_plan": ps})
                profile["selected_plan"] = ps

        rag_ctx = ""
        kb_recs  = []
        kb_reply = ""

        # ══════════════════════════════════════════════════════════════════════
        # RECOMMENDATION MEMORY — Plans shown ONCE per session only
        # ══════════════════════════════════════════════════════════════════════
        # Detect profile-change events that allow fresh recommendations
        _budget_changed  = (stage == "collect_budget" and extracted.get("budget_range"))
        _family_changed  = (stage == "collect_family" and extracted.get("family_members"))
        _edit_changed    = (stage == "edit_details" and bool(extracted))
        _fraud_completed = (stage in ("fraud_check", "risk_scoring"))  # pipeline just ran
        _profile_changed = _budget_changed or _family_changed or _edit_changed or _fraud_completed

        if _profile_changed:
            # Profile changed → reset plans_shown so new recs can fire
            self.db.clear_plans_shown(user_id)
            profile["plans_shown"] = 0

        _plans_mem       = self.db.get_plans_shown(user_id)
        _shown_names     = _plans_mem["plan_names"]
        # Validate stored plan names match current insurance type — clear if stale
        _current_ins = (profile.get("insurance_type") or "").lower()
        _plans_stale = False
        if _shown_names and _current_ins:
            # Detect obvious type mismatch: vehicle plans shown for health insurance etc.
            _plan_text = " ".join(_shown_names).lower()
            _is_vehicle_plan = any(w in _plan_text for w in ["motor","vehicle","comprehensive motor","automobile"])
            _is_health_plan  = any(w in _plan_text for w in ["health","optima","family","bupa","care plan","diabetes"])
            _is_term_plan    = any(w in _plan_text for w in ["protect","term","jeevan","iprotect"])
            if "vehicle" in _current_ins and (_is_health_plan or _is_term_plan):
                _plans_stale = True
            elif "health" in _current_ins and (_is_vehicle_plan or _is_term_plan):
                _plans_stale = True
            elif ("term" in _current_ins or "life" in _current_ins) and (_is_vehicle_plan or _is_health_plan):
                _plans_stale = True
        if _plans_stale:
            self.db.clear_plans_shown(user_id)
            _plans_mem = {"shown": False, "plan_names": []}
            _shown_names = []
        _plans_already   = _plans_mem["shown"] and not _profile_changed and not _plans_stale

        # Pass plans_already flag to build_prompt via profile
        profile["_plans_already_shown"] = _plans_already

        if next_stage == "recommendation":
            rag_ctx = self.rag.get_context(self._rag_query(profile), self.gemini)

            if not _plans_already:
                # ── FIRST TIME: run KB and generate recommendations ────────────
                try:
                    from models.policy_kb import PolicyKB
                    _pkb = PolicyKB(self.db, self.gemini)
                    profile_with_uid = dict(profile)
                    profile_with_uid["user_id"] = user_id
                    kb_recs = _pkb.get_recommendations(profile_with_uid, top_n=3)
                    if kb_recs:
                        kb_reply = _pkb.format_recommendation_text(kb_recs, profile_with_uid)
                        plan_names = [item["plan"].get("plan_name","") for item in kb_recs
                                      if item["plan"].get("plan_name")]
                        profile["_kb_plan_options"] = plan_names
                        # Mark plans as shown in DB
                        self.db.mark_plans_shown(user_id, plan_names)
                        # Store recommendations in DB
                        for item in kb_recs:
                            p = item["plan"]
                            self.db.store_recommendation(user_id, {
                                "name":          p.get("plan_name",""),
                                "premium":       p.get("premium_range",""),
                                "coverage":      p.get("coverage_amount",""),
                                "waiting_period": p.get("waiting_period",""),
                                "reason":        item.get("reason",""),
                            })
                except Exception as _kbe:
                    import logging
                    logging.getLogger("PolicyBot").warning(f"[KB] Recommendation error: {_kbe}")
            else:
                # ── ALREADY SHOWN: set plan names for options but NO new rec reply ──
                profile["_kb_plan_options"] = _shown_names

        elif next_stage == "explain_plan":
            rag_ctx = self.rag.get_context(self._rag_query(profile), self.gemini)

        prompt   = self._build_prompt(message, history, profile, rag_ctx, next_stage, language)
        # Token budget: review_details needs more room for full summary
        _tok = 900 if next_stage in ("review_details", "edit_details", "recommendation", "collect_budget") else 500
        ai_reply = self.gemini.generate(prompt, system_prompt=SYSTEM_PROMPT, max_tokens=_tok)

        # ── Fallback reply if AI fails or returns empty ──────────────────────────
        # Treat Gemini error strings same as empty (prevents error text showing as bot reply)
        _gemini_errors = [
            "sorry, our server is busy",
            "i'm having trouble connecting",
            "i encountered an issue",
            "please check your api keys",
        ]
        _is_ai_error = (not ai_reply or not ai_reply.strip() or
                        any(err in (ai_reply or "").lower() for err in _gemini_errors))
        if _is_ai_error:
            _name = profile.get("name", "")
            _ins  = (profile.get("insurance_type") or "insurance")
            _cond = profile.get("medical_conditions", "")
            _ins_l = _ins.lower()
            _fallback_map = {
                "collect_coverage":       f"Who would you like the insurance policy to cover{', ' + _name if _name else ''}? 😊 (Myself only / My spouse and children / Whole family)",
                "collect_family_count":   f"How many family members should be covered? 👨‍👩‍👧‍👦 (Please enter a number)",
                "collect_family_medical": f"Does anyone in your family have any existing medical conditions? 🏥 (No, everyone is healthy / Yes, there are medical conditions)",
                "collect_family_members": f"Please tell me the next family member's relationship and age. Example: Spouse, 40 😊",
                "collect_medical_status": f"Do you have any existing medical conditions{', ' + _name if _name else ''}? 🏥",
                "collect_medical":   (
                    f"Do you or any family members have any pre-existing injuries, "
                    "disabilities or occupational hazards? ⚡ (Select all that apply)"
                    if "accident" in (_ins or "").lower() else
                    f"Do you or any family members have any health history or lifestyle habits to declare? 📋 "
                    "(Select all that apply)"
                    if any(w in (_ins or "").lower() for w in ["term","life"]) else
                    f"Any medical conditions that may affect your travel coverage? ✈️ "
                    "(Select all that apply, or choose None)"
                    if "travel" in (_ins or "").lower() else
                    f"Please briefly mention your medical condition(s) 🏥 (Select all that apply)"
                ),
                "optional_medical_report": f"Since you have no conditions, uploading a health report helps us recommend better plans{', ' + _name if _name else ''} 📋 You can also skip this step.",
                "collect_budget":    f"Almost there{', ' + _name if _name else ''}! 🎯 What is your monthly budget for insurance premiums? 💰",
                "review_details":    f"Please review your details{', ' + _name if _name else ''} 📋 Name: {profile.get('name','—')} | Age: {profile.get('age','—')} | City: {profile.get('city','—')} | Insurance: {profile.get('insurance_type','—')} | Budget: {profile.get('budget_range','—')} | Medical: {profile.get('medical_conditions','None')} — Would you like to continue with these details? 😊",
                "edit_details":      f"Sure{', ' + _name if _name else ''}! Which details would you like to update? 😊 You can change: Location, Coverage, Medical conditions, or Budget.",
                "collect_family":    f"Who would you like to include in your insurance coverage{', ' + _name if _name else ''}? 👨‍👩‍👧‍👦",
                "collect_city":      f"Which city do you live in{', ' + _name if _name else ''}? 🏙️",
                "ask_escalation":    f"Would you like to speak with a human advisor, {_name or 'there'}? 😊",
                "ask_rating":        f"Thanks for chatting with me{', ' + _name if _name else ''}! 😊 Could you rate our conversation from 1 to 5 stars? ✨",
                "farewell":          f"🎉 Thank you {_name or ''}! Your insurance journey is complete. Wishing you a safe and secure future!",
            }
            ai_reply = _fallback_map.get(next_stage, "")

        # ── Build final reply ──────────────────────────────────────────────────
        if next_stage == "recommendation":
            if kb_reply and not _plans_already:
                # Fresh recommendation — use KB reply
                reply = kb_reply
            elif _plans_already:
                # Plans already shown — AI handles "none/ok thanks/repeat" gracefully
                reply = ai_reply or ""
            else:
                reply = ai_reply or ""

        elif next_stage == "explain_plan":
            reply = ai_reply or ""
            if reply and "apply now" not in reply.lower() and "compare other plans" not in reply.lower():
                reply += "\n\nWould you like to apply now or compare other plans?"
        else:
            reply = ai_reply
        options, opt_type = self._options(next_stage, profile)

        # Show upload card only for stages where user needs to upload a file
        show_upload = next_stage in (
            "doc_upload", "verify_wait",
            "optional_medical_report",        # NEW — optional medical report upload
            "condition_report_upload", "condition_report_wait",
            "optional_health_check",
            "vehicle_doc_upload",   # vehicle_history itself is radio-only (no upload needed)
            "life_docs",
        )
        # travel_declare and property_history are radio-only (no upload widget needed)
        # vehicle_history is radio-only — upload comes AFTER if needed (vehicle_doc_upload)

        _is_farewell    = next_stage == "farewell"
        _lock_chat      = _is_farewell  # frontend should lock all input

        # Build structured plan data for comparison table (only at recommendation stage)
        _plans_table = []
        if next_stage == "recommendation" and kb_recs:
            for _item in kb_recs:
                _p = _item["plan"]
                _plans_table.append({
                    "name":        _p.get("plan_name", ""),
                    "company":     _p.get("company_name", ""),
                    "type":        _p.get("insurance_type", ""),
                    "coverage":    _p.get("coverage_amount", "N/A"),
                    "premium":     _p.get("premium_range", "N/A"),
                    "waiting":     _p.get("waiting_period", "N/A"),
                    "age":         _p.get("eligibility_age", "N/A"),
                    "benefits":    (_p.get("special_benefits") or "")[:120],
                    "reason":      _item.get("reason", ""),
                    "score":       _item.get("score", 0),
                })

        return {
            "reply":              reply,
            "stage":              next_stage,
            "options":            [] if _is_farewell else options,
            "option_type":        "none" if _is_farewell else opt_type,
            "profile_updated":    bool(extracted),
            "show_rating":        next_stage == "ask_rating",
            "show_escalation":    next_stage == "ask_escalation",
            "show_farewell":      _is_farewell,
            "show_upload":        show_upload,
            "confidence":         self._confidence(profile, next_stage),
            "progress":           self.PROGRESS.get(next_stage, 7),
            "stage_label":        self.LABELS.get(next_stage, ""),
            "module":             self._module(next_stage),
            "plans_mentioned":    [],
            "plans_table":        _plans_table,
            "trigger_cleanup":    _is_farewell,
            "lock_chat":          _lock_chat,
        }

    # ══════════════════════════════════════════════════════
    # STAGE TRANSITIONS
    # ══════════════════════════════════════════════════════
    # Words that mean "I want to end this session now"
    _WIND_UP_WORDS = [
        "wind up","wind-up","windup","wrap up","wrap-up","wrapup",
        "end session","close session","end chat","close chat",
        "finish","done talking","bye","goodbye","cya","see ya",
        "thank you bye","thanks bye","thats all","that's all","all done",
        "exit","quit","im done","i'm done","no more","i'm good","im good",
        "good bye","goodbye","not interested","no thanks",
        "maybe later","not now","wind",
    ]

    def _next(self, stage, message, extracted, profile, user_id=""):
        msg = message.lower().strip()
        ins = (profile.get("insurance_type") or "").lower()
        is_skip = any(w in msg for w in _SKIP_WORDS)

        # ── Global wind-up detection — works from ANY stage ────────────────
        # Skip early stages where "wind" could be a name or city
        _safe_wind_stages = {"collect_budget","recommendation","explain_plan",
                             "ask_escalation","vehicle_history","vehicle_doc_upload",
                             "life_docs","travel_declare","property_history",
                             "optional_health_check","condition_report_upload",
                             "collect_coverage","collect_family_count","collect_family_medical",
                             "collect_medical_status","optional_medical_report",
                             "review_details","edit_details"}
        if stage in _safe_wind_stages:
            if any(w in msg for w in self._WIND_UP_WORDS):
                return "ask_rating"

        # ── Linear stages 1-7 (unchanged) ─────────────────────────────────
        if stage == "insurance_type":
            ins = extracted.get("insurance_type") or profile.get("insurance_type")
            if ins:
                # Clear any stale plan cache from previous session
                self.db.clear_plans_shown(user_id)
                # If name AND age were ALSO in the same opening message, skip straight to doc_upload
                has_name = extracted.get("name") or profile.get("name")
                has_age  = extracted.get("age")  or profile.get("age")
                if has_name and has_age:
                    return "doc_upload"
                if has_name:
                    return "collect_age"
                return "collect_name"
            return "insurance_type"

        if stage == "collect_name":
            # Skip if name was pre-extracted from this or a previous message
            if extracted.get("name") or profile.get("name"):
                # If age is ALSO already known (smart-extracted in same message), jump straight to doc_upload
                if profile.get("age") or extracted.get("age"):
                    return "doc_upload"
                return "collect_age"
            return "collect_name"

        if stage == "collect_age":
            # Skip if age was pre-extracted
            if profile.get("age") or extracted.get("age"):
                return "doc_upload"
            return "collect_age"

        if stage == "doc_upload":
            if is_skip:
                # ID skipped — no gender from ID, but we still don't ask gender manually
                self.db.upsert_user_profile(user_id, {"gov_id_verified": 0})
                return "collect_city"   # gender never asked as a question
            return "doc_upload"

        if stage == "verify_wait":
            return "verify_wait"  # only /api/upload exits

        if stage == "collect_gender":
            # Gender is auto-extracted from Gov ID — NEVER asked as a manual question.
            # This stage is only reached if gender wasn't on the ID (e.g. PAN card).
            # In all cases: just advance to city. Do NOT prompt for gender input.
            if profile.get("gender") or extracted.get("gender"):
                return "collect_city"
            # Even without gender — skip to city (gender not required for flow)
            return "collect_city"

        if stage == "collect_city":
            # Skip if city was pre-extracted
            if profile.get("city") and not extracted.get("city"):
                return "collect_coverage"
            return "collect_coverage" if extracted.get("city") else "collect_city"

        # ── Step 8: Coverage selection ─────────────────────────────────────
        if stage == "collect_coverage":
            cov = extracted.get("coverage_type") or profile.get("coverage_type")
            if not cov:
                return "collect_coverage"
            # "Myself only" → skip all family collection
            if "myself" in cov.lower() or "only" in cov.lower():
                return "collect_medical_status"
            # Family coverage → collect member count first
            return "collect_family_count"

        # ── Step 9: How many family members ────────────────────────────────
        if stage == "collect_family_count":
            count = extracted.get("family_member_count")
            if count is None:
                count = profile.get("family_member_count")
            if not count:
                return "collect_family_count"
            # After getting count — ask family medical condition (NOT per-member details)
            return "collect_family_medical"

        # ── Step 10: Does anyone in the family have a medical condition? ──
        # Single question, replaces iterative per-member collection
        if stage == "collect_family_medical":
            fam_med = extracted.get("family_medical_conditions")
            if not fam_med:
                return "collect_family_medical"
            # Family medical answered — now ask main user's medical status
            return "collect_medical_status"

        # ── Legacy collect_family_members (kept for existing sessions) ─────
        if stage == "collect_family_members":
            # Check how many members collected vs total needed
            needed   = int(profile.get("family_member_count") or 1)
            existing = profile.get("family_members_json") or "[]"
            try:
                members = json.loads(existing)
            except Exception:
                members = []
            new_member = extracted.get("family_member_entry")
            if new_member:
                members.append(new_member)
                self.db.upsert_user_profile(user_id, {
                    "family_members_json": json.dumps(members),
                    "family_members": ", ".join(
                        f"{m['relationship']} ({m['age']})" for m in members
                    ),
                })
                profile["family_members_json"] = json.dumps(members)
                profile["family_members"]       = profile.get("family_members", "")
            if len(members) < needed:
                return "collect_family_members"
            return "collect_medical_status"

        # ── Legacy collect_family (keep for existing sessions) ────────────
        if stage == "collect_family":
            if not extracted.get("family_members"):
                return "collect_family"
            return "collect_medical_status"

        # ── Step 12: Medical status yes/no gate ───────────────────────────
        if stage == "collect_medical_status":
            status = extracted.get("medical_conditions_status")
            if not status:
                return "collect_medical_status"
            if status == "None":
                # No conditions — go straight to optional medical report
                return "optional_medical_report"
            # Has conditions → collect details
            return "collect_medical"

        # ── Step 13: collect_medical → always go to optional_medical_report first
        if stage == "collect_medical":
            if not extracted.get("medical_conditions"):
                return "collect_medical"
            # Route through optional medical report before type-specific branch
            return "optional_medical_report"

        # ── Step 13a: Optional medical report upload ──────────────────────
        if stage == "optional_medical_report":
            # Upload handled by /api/upload — it sets next_stage
            if is_skip or "no" in msg or "skip" in msg:
                # No report uploaded — go to insurance-type-specific branch
                return self._medical_branch(profile, user_id)
            # Wait for upload
            return "optional_medical_report"

        # ── Condition report upload (locked — /api/upload exits to collect_budget) ──
        if stage == "condition_report_upload":
            # Stays here until /api/upload sets stage to collect_budget
            # Skip allowed
            if is_skip:
                self.db.upsert_user_profile(user_id,
                    {"condition_report_uploaded": 0, "onboarding_stage": "collect_budget"})
                return "collect_budget"
            return "condition_report_upload"

        if stage == "condition_report_wait":
            return "condition_report_wait"  # only /api/upload exits

        # ── Optional health check (fully skippable) ─────────────────────────
        if stage == "optional_health_check":
            if is_skip or "no" in msg or "none" in msg:
                return "collect_budget"
            # Upload triggers /api/upload → advances to collect_budget
            return "optional_health_check"

        # ── Vehicle history ─────────────────────────────────────────────────
        if stage == "vehicle_history":
            hist = extracted.get("vehicle_history", "")
            if hist:
                if "none" in hist.lower():
                    return "collect_budget"
                # Has previous policy or accident → ask for doc upload
                return "vehicle_doc_upload"
            return "vehicle_history"

        if stage == "vehicle_doc_upload":
            if is_skip:
                return "collect_budget"
            return "vehicle_doc_upload"

        # ── Life docs ───────────────────────────────────────────────────────
        if stage == "life_docs":
            if is_skip or "none" in msg:
                return "collect_budget"
            return "life_docs"

        # ── Travel declare ──────────────────────────────────────────────────
        if stage == "travel_declare":
            if is_skip or "none" in msg:
                return "collect_budget"
            return "travel_declare"

        # ── Property history ────────────────────────────────────────────────
        if stage == "property_history":
            if is_skip or "none" in msg:
                return "collect_budget"
            return "property_history"

        # ── Steps 10-15 (unchanged) ─────────────────────────────────────────
        if stage == "collect_budget":
            return "review_details" if extracted.get("budget_range") else "collect_budget"

        # ── Review details — show summary, let user confirm or edit ────────
        if stage == "review_details":
            if is_skip or any(w in msg for w in ["continue","yes","confirm","correct",
                                                  "proceed","ok","okay","sure","looks good",
                                                  "1","go ahead","right","all good",
                                                  "✅ continue"]):
                # Mark confirmed, run risk pipeline silently, then go to recommendation
                self.db.upsert_user_profile(user_id, {"review_confirmed": 1})
                try:
                    from models.risk_engine import run_risk_pipeline
                    run_risk_pipeline(profile, self.db, user_id)
                except Exception as _re:
                    import logging as _log
                    _log.getLogger("PolicyBot").warning(f"[RISK] pipeline error: {_re}")
                return "recommendation"
            if any(w in msg for w in ["change","edit","update","modify","wrong",
                                       "incorrect","no","2","different",
                                       "✏️ change details"]):
                return "edit_details"
            return "review_details"

        # ── Edit details — let user update specific fields ──────────────────
        if stage == "edit_details":
            # Any extracted profile update is saved in process(); then re-show review
            # If user says continue/done → back to review to re-confirm
            if any(w in msg for w in ["done","continue","save","ok","okay","confirmed",
                                       "that's all","thats all","finish","proceed"]):
                return "review_details"
            # If user provides updated data (city, budget, medical, coverage) → stay
            updated_any = bool(
                extracted.get("city") or extracted.get("budget_range") or
                extracted.get("medical_conditions") or extracted.get("coverage_type") or
                extracted.get("family_medical_conditions")
            )
            # After extracting updates → go back to review to re-confirm
            if updated_any:
                return "review_details"
            return "edit_details"

        # ── Fraud Check: SILENT — auto-advance, no user message needed ───────
        if stage == "fraud_check":
            return "risk_scoring"

        # ── Risk Scoring: SILENT — auto-advance, no user message needed ──────
        if stage == "risk_scoring":
            return "recommendation"

        if stage == "recommendation":
            select_words = ["select","choose","this one","go with","i want","apply",
                            "plan 1","plan 2","plan 3","option 1","first","second","1st","2nd"]
            # User selected a plan → explain it
            if self._detect_plan(message) or any(w in msg for w in select_words):
                return "explain_plan"
            # User wants a human advisor
            if any(w in msg for w in ["advisor","human","agent","call me","speak to"]):
                return "ask_escalation"
            # User wants to END the session
            _exit_words = ["wind up","wind-up","windup","wind","wrap up","wrap-up","wrapup","wrap",
                           "end session","close session","finish","done","bye","goodbye","cya",
                           "no thank","not interested","exit","close","end","quit",
                           "thank you","thanks bye","thats all","that's all","all done",
                           "nothing","nothin","no more","im good","i'm good","good bye",
                           "no plan","not now","maybe later","later","not needed","not interested"]
            if any(w in msg for w in _exit_words):
                return "ask_rating"   # Skip to rating → farewell
            return "recommendation"

        if stage == "explain_plan":
            _exit_words2 = ["wind up","wind-up","windup","wind","wrap up","wrap","done","bye","goodbye",
                             "no thanks","finish","end","quit","exit","nothin","nothing","no more",
                             "thank you","thanks","all done","thats all","i'm good","im good"]
            if any(w in msg for w in _exit_words2):
                return "ask_rating"
            return "ask_escalation"

        if stage == "ask_escalation":
            # Always advance to rating after escalation decision
            return "ask_rating"

        if stage == "ask_rating":
            # Advance to farewell if: numeric rating given OR any acknowledgment
            _ack_words = ["ok","okay","okk","thanks","thank","fine","done","bye","great","good","no","skip","later"]
            if self._extract_rating(message) or any(w in msg for w in _ack_words):
                return "farewell"
            return "ask_rating"

        if stage == "farewell":
            return "farewell"

        return stage

    # ══════════════════════════════════════════════════════
    # MEDICAL BRANCH HELPER
    # Called after optional_medical_report skip/upload
    # Routes to the correct insurance-type-specific branch
    # ══════════════════════════════════════════════════════
    def _medical_branch(self, profile, user_id):
        """Determine the next stage after optional_medical_report based on
        insurance type, main user conditions, and family conditions."""
        ins       = (profile.get("insurance_type") or "").lower()
        cond      = (profile.get("medical_conditions") or "").lower()
        fam_cond  = (profile.get("family_medical_conditions") or "").lower()
        # Consider condition present if main user OR family has one
        has_condition      = bool(cond) and "none" not in cond
        has_family_cond    = bool(fam_cond) and "none" not in fam_cond
        any_condition      = has_condition or has_family_cond

        # Health Insurance
        if "health" in ins:
            if has_condition:
                # Main user has condition — upload condition report
                self.db.upsert_user_profile(user_id, {"condition_report_uploaded": 0})
                return "condition_report_upload"
            if has_family_cond:
                # Family has condition but main user is healthy — optional check still useful
                return "optional_health_check"
            # Nobody has conditions — optional general health check
            return "optional_health_check"

        # Vehicle Insurance
        if "vehicle" in ins:
            return "vehicle_history"

        # Life / Term Insurance
        if "life" in ins or "term" in ins:
            return "life_docs"

        # Travel Insurance
        if "travel" in ins:
            return "travel_declare"

        # Property Insurance
        if "property" in ins:
            return "property_history"

        # Accident Insurance
        if "accident" in ins:
            return "collect_budget"

        return "collect_budget"

    # ══════════════════════════════════════════════════════
    # STAGE-GATED EXTRACTION
    # ══════════════════════════════════════════════════════
    def _extract(self, message, stage):
        msg = message.lower().strip()
        out = {}

        if stage == "insurance_type":
            ins_map = {
                "health":"Health Insurance", "term":"Term / Life Insurance",
                "life":"Term / Life Insurance", "vehicle":"Vehicle Insurance",
                "car":"Vehicle Insurance", "motor":"Vehicle Insurance",
                "bike":"Vehicle Insurance", "travel":"Travel Insurance",
                "property":"Property Insurance", "home":"Property Insurance",
                "accident":"Accident Insurance",
            }
            for k, v in ins_map.items():
                if k in msg:
                    out["insurance_type"] = v
                    break

        elif stage == "collect_name":
            import re as _re
            text = message.strip()
            # Strip common name-introduction prefixes so "Call me Suresh" → "Suresh"
            _prefix = _re.sub(
                r"^(?:call\s+me|i\s+am|i'm|im|my\s+name\s+is|this\s+is|name\s+is)\s+",
                '', text, flags=_re.IGNORECASE
            ).strip()
            # Remove trailing noise ("and I am ...", "age ...", "from ...", etc.)
            _prefix = _re.sub(
                r'\s+(?:and|my|age|aged|i|is|want|looking|years?|old|from|in|at).*',
                '', _prefix, flags=_re.IGNORECASE
            ).strip()
            # Use cleaned version if it differs (i.e. a prefix was found)
            candidate = _prefix if _prefix and _prefix.lower() != text.lower() else text
            if candidate.lower() not in _NOT_A_NAME:
                words = candidate.split()
                if (1 <= len(words) <= 4
                        and candidate[0].isalpha()
                        and not any(c.isdigit() for c in candidate)
                        and candidate.lower() not in _NOT_A_NAME
                        and not any(kw in candidate.lower() for kw in
                                    ["insurance","health","vehicle","travel","property","accident","term"])):
                    out["name"] = candidate.title()

        elif stage == "collect_age":
            nums = re.findall(r'\b(\d{1,3})\b', message)
            for n in nums:
                v = int(n)
                if 1 <= v <= 120:
                    out["age"] = v
                    break

        elif stage == "collect_gender":
            if message.strip() in ["Male", "Female", "Other"]:
                out["gender"] = message.strip()
            elif any(w in msg for w in ["male","man","boy"]):
                out["gender"] = "Male"
            elif any(w in msg for w in ["female","woman","girl"]):
                out["gender"] = "Female"
            elif "other" in msg:
                out["gender"] = "Other"

        elif stage == "collect_city":
            text = message.strip()
            if text and len(text) >= 2 and not text.isdigit() and text.lower() not in _NOT_A_NAME:
                out["city"] = text.title()

        elif stage == "collect_coverage":
            cov_map = {
                "myself only":    "Myself only",
                "only myself":    "Myself only",
                "just me":        "Myself only",
                "only me":        "Myself only",
                "myself":         "Myself only",
                "spouse and children": "My spouse and children",
                "spouse & children":   "My spouse and children",
                "spouse and kids":     "My spouse and children",
                "wife and kids":       "My spouse and children",
                "family and children": "My spouse and children",
                "whole family":   "Whole family",
                "entire family":  "Whole family",
                "full family":    "Whole family",
                "all family":     "Whole family",
                "family":         "Whole family",
                "everyone":       "Whole family",
            }
            for phrase, value in sorted(cov_map.items(), key=lambda x: -len(x[0])):
                if phrase in msg:
                    out["coverage_type"] = value
                    break
            # Numeric shortcut: "1" → Myself only, "2" → spouse+children, "3" → whole family
            if not out.get("coverage_type"):
                if msg.strip() == "1":
                    out["coverage_type"] = "Myself only"
                elif msg.strip() == "2":
                    out["coverage_type"] = "My spouse and children"
                elif msg.strip() == "3":
                    out["coverage_type"] = "Whole family"

        elif stage == "collect_family_count":
            nums = re.findall(r'\b(\d{1,2})\b', message)
            for n in nums:
                v = int(n)
                if 1 <= v <= 20:
                    out["family_member_count"] = v
                    break
            # word numbers
            if not out.get("family_member_count"):
                word_map = {"one":1,"two":2,"three":3,"four":4,"five":5,
                            "six":6,"seven":7,"eight":8,"nine":9,"ten":10}
                for w, v in word_map.items():
                    if w in msg:
                        out["family_member_count"] = v
                        break

        elif stage == "collect_family_medical":
            # "Does anyone in the family have a medical condition?" — yes/no + brief mention
            no_keywords  = ["no ", "none", "healthy", "nothing", "no condition",
                            "fit", "nope", "nah", "not any", "no medical", "no one"]
            if any(kw in msg for kw in no_keywords) or msg.strip() in ("no", "1", "none"):
                out["family_medical_conditions"] = "None"
            else:
                # Any other answer — treat as description of condition(s)
                out["family_medical_conditions"] = message.strip() or "None"

        elif stage == "collect_family_members":
            # Expect "Spouse, 40" or "Child, 12" or "relationship: X age: Y" style
            rel_map = {
                "spouse":"Spouse", "wife":"Spouse", "husband":"Spouse",
                "son":"Son", "daughter":"Daughter",
                "child":"Child", "kid":"Child",
                "father":"Father", "dad":"Father",
                "mother":"Mother", "mom":"Mother",
                "parent":"Parent", "sibling":"Sibling",
                "brother":"Brother", "sister":"Sister",
                "grandfather":"Grandfather", "grandmother":"Grandmother",
                "grandparent":"Grandparent",
            }
            rel_found = None
            for k, v in rel_map.items():
                if k in msg:
                    rel_found = v
                    break
            # Extract age
            age_found = None
            age_m = re.search(r'\b(\d{1,3})\b', message)
            if age_m:
                av = int(age_m.group(1))
                if 1 <= av <= 120:
                    age_found = av
            if rel_found and age_found:
                out["family_member_entry"] = {"relationship": rel_found, "age": age_found}
            elif rel_found:
                out["family_member_entry"] = {"relationship": rel_found, "age": None}

        elif stage == "collect_medical_status":
            # Yes/No gate — maps to "None" or "HasConditions"
            no_keywords  = ["no ", "none", "no existing", "no condition", "healthy",
                            "nothing", "fit", "nope", "nah", "not any", "no medical"]
            yes_keywords = ["yes", "have ", "has ", "condition", "disease", "medical",
                            "suffer", "diagnosed", "diabetes", "heart", "blood pressure",
                            "asthma", "cancer", "thyroid", "kidney", "hypertension"]
            # Check "No" first (more specific phrases)
            if any(kw in msg for kw in no_keywords) or msg.strip() in ("no", "1"):
                out["medical_conditions_status"] = "None"
                out["medical_conditions"]        = "None"
            elif any(kw in msg for kw in yes_keywords) or msg.strip() == "2":
                out["medical_conditions_status"] = "HasConditions"

        elif stage == "collect_family":
            fam_map = {
                "only me":"Only Me","just me":"Only Me","myself":"Only Me",
                "spouse":"Spouse","wife":"Spouse","husband":"Spouse",
                "child":"Children","children":"Children","kids":"Children",
                "parent":"Parents","mother":"Parents","father":"Parents",
                "family":"Full Family","everyone":"Full Family","full family":"Full Family",
            }
            found = []
            for k, v in fam_map.items():
                if k in msg and v not in found:
                    found.append(v)
            if found:
                out["family_members"] = ", ".join(found)
            elif message.strip() and message.strip().lower() not in _NOT_A_NAME:
                out["family_members"] = message.strip()

        elif stage == "collect_medical":
            # Comprehensive map covering all insurance types' conditions
            med_map = {
                # Health conditions
                "diabetes":"Diabetes","sugar":"Diabetes",
                "blood pressure":"Blood Pressure","hypertension":"Blood Pressure",
                "bp ":"Blood Pressure","high bp":"Blood Pressure",
                "heart":"Heart Disease","cardiac":"Heart Disease",
                "asthma":"Asthma","kidney":"Kidney Disease","renal":"Kidney Disease",
                "cancer":"Cancer","thyroid":"Thyroid",
                # Term/Life conditions
                "smoking":"Smoking / Tobacco","tobacco":"Smoking / Tobacco","smoke":"Smoking / Tobacco",
                "alcohol":"Alcohol Use","drink":"Alcohol Use",
                "cancer history":"Cancer History",
                # Accident conditions
                "injury":"Previous Injury","injured":"Previous Injury",
                "disability":"Permanent Disability","disabled":"Permanent Disability",
                "fracture":"Fracture History","broken":"Fracture History",
                "sport":"Sports Injury",
                "occupational":"Occupational Hazard","hazard":"Occupational Hazard",
                # Travel conditions
                "pregnant":"Pregnancy","pregnancy":"Pregnancy",
                "surgery":"Recent Surgery","operated":"Recent Surgery",
                "respiratory":"Respiratory Issues","breathing":"Respiratory Issues",
                "heart condition":"Heart Condition",
                # Generic
                "other":"Other",
                "none":"None","no ":"None","healthy":"None","nothing":"None","fit":"None","nope":"None",
            }
            found = []
            for k, v in med_map.items():
                if k in msg and v not in found:
                    found.append(v)
            if found:
                out["medical_conditions"] = ", ".join(found)
            elif message.strip() and message.strip().lower() not in _NOT_A_NAME:
                out["medical_conditions"] = message.strip()

        elif stage == "vehicle_history":
            vh_map = {
                "previous policy":"Previous Policy",
                "prev policy":"Previous Policy",
                "accident":"Accident Claim",
                "claim":"Accident Claim",
                "none":"None",
                "no ":"None","nothing":"None",
            }
            found = []
            for k, v in vh_map.items():
                if k in msg and v not in found:
                    found.append(v)
            if found:
                out["vehicle_history"] = ", ".join(found)
            elif message.strip() and message.strip().lower() not in _NOT_A_NAME:
                out["vehicle_history"] = message.strip()

        elif stage == "life_docs":
            ld_map = {
                "medical":"Medical History",
                "income":"Income Proof",
                "salary":"Income Proof",
                "none":"None","no ":"None",
            }
            found = []
            for k, v in ld_map.items():
                if k in msg and v not in found:
                    found.append(v)
            if found:
                out["life_docs"] = ", ".join(found)
            elif message.strip() and message.strip().lower() not in _NOT_A_NAME:
                out["life_docs"] = message.strip()

        elif stage == "travel_declare":
            td_map = {
                "medical":"Medical Condition",
                "trip":"Trip Delay History",
                "delay":"Trip Delay History",
                "none":"None","no ":"None",
            }
            found = []
            for k, v in td_map.items():
                if k in msg and v not in found:
                    found.append(v)
            if found:
                out["travel_declare"] = ", ".join(found)
            elif message.strip() and message.strip().lower() not in _NOT_A_NAME:
                out["travel_declare"] = message.strip()

        elif stage == "property_history":
            ph_map = {
                "fire":"Fire Damage",
                "flood":"Flood Damage",
                "claim":"Previous Claim",
                "damage":"Previous Claim",
                "none":"None","no ":"None",
            }
            found = []
            for k, v in ph_map.items():
                if k in msg and v not in found:
                    found.append(v)
            if found:
                out["property_history"] = ", ".join(found)
            elif message.strip() and message.strip().lower() not in _NOT_A_NAME:
                out["property_history"] = message.strip()

        elif stage == "collect_budget":
            msg_stripped = message.strip()
            # Only save as budget if it looks like an actual budget value
            # Reject generic confirm words that get sent after budget was already saved
            _budget_noise = {"ok","okay","sure","proceed","continue","yes","no",
                             "confirm","correct","next","go","done","right",
                             "looks good","all good","go ahead","great","fine",
                             "budget","premium","monthly","amount","range","cost",
                             "ok proceed","ok proeed","proeed","hmm","yeah","yep",
                             "what","skip","please","now","let","let's","lets"}
            if msg_stripped and msg_stripped.lower() not in _budget_noise:
                out["budget_range"] = msg_stripped

        elif stage == "edit_details":
            # Allow user to update any of: city, coverage_type, medical_conditions, budget_range, family_medical_conditions
            msg_l = message.lower().strip()
            # City update: "update city to Chennai" or "Chennai" alone
            city_match = re.search(
                r'(?:city|location|from|to|in|at)\s+([a-zA-Z][a-zA-Z\s]{2,24})',
                message, re.IGNORECASE
            )
            if city_match:
                candidate = city_match.group(1).strip().title()
                words = candidate.split()
                if 1 <= len(words) <= 3 and not any(c.isdigit() for c in candidate):
                    out["city"] = candidate
            # Budget update: any budget keyword or amount
            budget_keywords = ["budget","premium","₹","rs ","inr","per month","monthly"]
            if any(k in msg_l for k in budget_keywords):
                out["budget_range"] = message.strip()
            # Medical update: "update medical to Diabetes" or just "Diabetes, Blood Pressure"
            med_match = re.search(
                r'(?:medical|condition|conditions|health)\s*(?:is|are|:)?\s*(.+)',
                message, re.IGNORECASE
            )
            if med_match:
                out["medical_conditions"] = med_match.group(1).strip().title()
            # Coverage update
            cov_map = {
                "myself only": "Myself only",
                "just me": "Myself only",
                "only me": "Myself only",
                "spouse": "My spouse and children",
                "children": "My spouse and children",
                "whole family": "Whole family",
                "entire family": "Whole family",
                "whole": "Whole family",
            }
            for k, v in cov_map.items():
                if k in msg_l:
                    out["coverage_type"] = v
                    break

        return out

    # ══════════════════════════════════════════════════════
    # OPTIONS — insurance-type-aware for condition branches
    # ══════════════════════════════════════════════════════
    def _options(self, stage, profile=None):
        ins = ((profile or {}).get("insurance_type") or "").lower()

        if stage == "insurance_type":
            return (["Health Insurance","Term / Life Insurance","Vehicle Insurance",
                     "Travel Insurance","Property Insurance","Accident Insurance"], "radio")

        if stage == "collect_gender":
            return (["Male","Female","Other"], "radio")

        # ── NEW: Coverage selection ──────────────────────────────────────
        if stage == "collect_coverage":
            return (["Myself only","My spouse and children","Whole family"], "radio")

        # ── Family count — no buttons, text input ───────────────────
        if stage == "collect_family_count":
            return ([], "none")

        # ── Family medical condition — yes/no radio ──────────────────
        if stage == "collect_family_medical":
            return (["No, everyone is healthy","Yes, there are medical conditions"], "radio")

        # ── NEW: Medical status yes/no gate ─────────────────────────────
        if stage == "collect_medical_status":
            return (["No existing medical conditions","Yes, there are medical conditions"], "radio")

        # ── NEW: Optional medical report upload ─────────────────────────
        if stage == "optional_medical_report":
            return (["Upload medical report","Skip"], "radio")

        if stage == "collect_family":
            return (["Only Me","Spouse","Children","Parents","Full Family"], "multi")

        if stage == "collect_medical":
            # ── Per-insurance-type condition options ────────────────────────
            if "vehicle" in ins:
                return ([], "none")           # vehicle → vehicle_history
            if "property" in ins:
                return ([], "none")           # property → property_history
            if "accident" in ins:
                # Accident: ask about injuries, disabilities, hazards
                return (["Previous Injury","Permanent Disability","Fracture History",
                         "Sports Injury","Occupational Hazard","None"], "multi")
            if "term" in ins or "life" in ins:
                # Term/Life: lifestyle and critical illness history
                return (["Smoking / Tobacco","Alcohol Use","Diabetes",
                         "Heart Disease","High Blood Pressure","Cancer History","None"], "multi")
            if "travel" in ins:
                # Travel: conditions affecting travel coverage
                return (["Diabetes","Heart Condition","Respiratory Issues",
                         "Pregnancy","Recent Surgery","None"], "multi")
            # Health Insurance (default)
            return (["Diabetes","Blood Pressure","Heart Disease",
                     "Asthma","Kidney Disease","Cancer","Thyroid","None"], "multi")

        if stage == "vehicle_history":
            return (["Previous Policy","Accident Claim","None"], "radio")

        if stage == "life_docs":
            return (["Medical History","Income Proof","None"], "radio")

        if stage == "travel_declare":
            return (["Medical Condition","Trip Delay History","None"], "radio")

        if stage == "property_history":
            return (["Fire Damage","Flood Damage","Previous Claim","None"], "radio")

        if stage == "collect_budget":
            return (["Under ₹500","₹500–₹1,000","₹1,000–₹2,000",
                     "₹2,000–₹5,000","Above ₹5,000"], "radio")

        if stage == "review_details":
            return (["✅ Continue","✏️ Change Details"], "radio")

        if stage == "edit_details":
            return (["📍 Update Location","🏥 Update Medical Conditions",
                     "💰 Update Budget","👨‍👩‍👧 Update Coverage",
                     "✅ Done, Show Review"], "radio")

        if stage == "recommendation":
            # Build plan radio options from KB recommendations cache
            # The actual recommendation text is built by policy_kb, options are plan names
            plans = (profile or {}).get("_kb_plan_options", [])
            if not plans:
                # Fallback plans — type AND condition specific
                # Normalize: "Smoking / Tobacco, Alcohol Use" → {"smoking","tobacco","alcohol use",...}
                _fb_ins   = ((profile or {}).get("insurance_type") or "").lower()
                _raw_cond = ((profile or {}).get("medical_conditions") or "").lower()
                # Split by comma, strip, flatten slash-separated terms into a set
                _cond_set = set()
                for _part in _raw_cond.split(","):
                    for _sub in _part.split("/"):
                        _cond_set.add(_sub.strip())

                def _has(*keywords):
                    """Check if any keyword appears in any condition token."""
                    return any(kw in token for kw in keywords for token in _cond_set)

                if "health" in _fb_ins:
                    if _has("diabetes"):
                        plans = ["Star Diabetes Safe","Niva Bupa ReAssure 360°","Care Freedom Plan"]
                    elif _has("heart","blood pressure","hypertension","cardiac"):
                        plans = ["Aditya Birla Activ Health Enhanced","Care Heart Plan","Star Cardiac Care"]
                    elif _has("cancer","critical"):
                        plans = ["HDFC Ergo Critical Illness","ICICI Pru Heart & Cancer","Bajaj Allianz CritiCare"]
                    elif _has("kidney","renal"):
                        plans = ["Star Health Medi Classic","Niva Bupa Senior First","Care Advantage Plan"]
                    elif _has("asthma","respiratory","lung"):
                        plans = ["HDFC Ergo Optima Restore","Niva Bupa Health Companion","Star Comprehensive"]
                    elif _has("thyroid"):
                        plans = ["Star Health Comprehensive","Care Classic Plan","HDFC Ergo My Health Suraksha"]
                    else:
                        plans = ["Star Health Family Optima","HDFC Ergo Optima Restore","Niva Bupa Health Companion"]
                elif "life" in _fb_ins or "term" in _fb_ins:
                    if _has("smoking","tobacco"):
                        plans = ["HDFC Click2Protect Plus","ICICI Pru iProtect Smart","Max Life Smart Secure"]
                    elif _has("diabetes","heart","cancer"):
                        plans = ["HDFC Click2Protect Life","Tata AIA Sampoorna Raksha","Bajaj Allianz Smart Protect"]
                    else:
                        plans = ["HDFC Click2Protect Life","LIC Tech Term","Tata AIA Sampoorna Raksha"]
                elif "vehicle" in _fb_ins:
                    _vh = ((profile or {}).get("vehicle_history") or "").lower()
                    if "accident" in _vh or "claim" in _vh:
                        plans = ["HDFC ERGO Motor Optima (Accident Cover)","Bajaj Allianz OD Cover","New India Motor Floater"]
                    elif "previous policy" in _vh or "prev policy" in _vh:
                        plans = ["ICICI Lombard Complete Cover","HDFC ERGO Motor Optima","Bajaj Allianz Comprehensive Motor"]
                    else:
                        plans = ["Bajaj Allianz Comprehensive Motor","ICICI Lombard Complete Cover","HDFC ERGO Motor Optima"]
                elif "travel" in _fb_ins:
                    if _has("diabetes","heart","pregnancy","surgery"):
                        plans = ["Bajaj Allianz Travel Care","HDFC ERGO Travel Protect","Tata AIG Overseas Care"]
                    else:
                        plans = ["Bajaj Allianz Travel Companion","HDFC ERGO Travel Protect","Tata AIG Travel Guard"]
                elif "property" in _fb_ins:
                    plans = ["New India Home Insurance","HDFC ERGO Home Protect","Bajaj Allianz Property Guard"]
                elif "accident" in _fb_ins:
                    if _has("injury","disability","fracture"):
                        plans = ["New India Janata Personal Accident","Bajaj Allianz Personal Guard","Star Accident Care"]
                    else:
                        plans = ["New India Personal Accident","Bajaj Allianz Personal Guard","Tata AIG Accident Cover"]
                else:
                    plans = ["Star Health Family Optima","HDFC Click2Protect Life","Bajaj Allianz Comprehensive Motor"]
            return (plans, "radio")

        if stage == "explain_plan":
            return (["Apply Now 🚀","Compare Other Plans 🔄","Talk to Advisor 👨‍💼"], "radio")

        if stage == "ask_escalation":
            return (["Talk to Human Advisor 👨‍💼","Continue with PolicyBot 🤖"], "radio")

        return [], "none"

    # ══════════════════════════════════════════════════════
    # PROMPT BUILDER
    # ══════════════════════════════════════════════════════
    def _build_prompt(self, message, history, profile, rag_ctx, stage, language):
        name = profile.get("name","")
        ins  = profile.get("insurance_type","insurance")
        cond = profile.get("medical_conditions","")

        safe_p = {k:v for k,v in {
            "name":             profile.get("name",""),
            "insurance_type":   profile.get("insurance_type",""),
            "age":              profile.get("age",""),
            "gender":           profile.get("gender",""),
            "city":             profile.get("city",""),
            "coverage_type":    profile.get("coverage_type",""),
            "family_member_count": profile.get("family_member_count",""),
            "family_members":   profile.get("family_members",""),
            "family_medical_conditions": profile.get("family_medical_conditions",""),
            "medical_conditions_status": profile.get("medical_conditions_status",""),
            "medical_conditions":profile.get("medical_conditions",""),
            "budget_range":     profile.get("budget_range",""),
            "risk_score":       profile.get("risk_score",""),
            "risk_category":    profile.get("risk_category",""),
            "premium_prediction": profile.get("premium_prediction",""),
            "fraud_status":     profile.get("fraud_status",""),
            "gov_id_verified":  profile.get("gov_id_verified",0),
            "selected_plan":    profile.get("selected_plan",""),
            "condition_report_uploaded": profile.get("condition_report_uploaded",0),
            "medical_report_uploaded":   profile.get("medical_report_uploaded",0),
        }.items() if v not in ("",None,0)}

        recent = "\n".join(
            f"User: {h.get('message','')}\nBot: {h.get('bot_reply','')}"
            for h in history[-5:]
        )

        # ── Conversation Memory context ─────────────────────────────────────
        _mem_ctx = memory_manager.get_context_summary(profile.get("user_id",""))
        _mem_should_skip = {
            k: memory_manager.get(profile.get("user_id","")).should_skip_question(k)
            for k in ["name","age","city","insurance_type","coverage_type","budget_range","medical_conditions"]
        }

        instructions = {
            # ── Original steps 1-10 ──────────────────────────────────────────
            "insurance_type":(
                "STEP 1: Welcome warmly. Ask what insurance they need. "
                "Say: 'Hi 😊 I'm PolicyBot, your AI insurance advisor! "
                "What type of insurance are you looking for today?' "
                "Insurance type radio buttons will appear automatically."
            ),
            "collect_name":(
                f"STEP 2: User chose '{ins}'. "
                + (
                    f"Smart extraction already found name='{profile.get('name','')}'. "
                    f"Acknowledge it warmly: 'Nice to meet you, {profile.get('name','')}! 😊 "
                    f"How old are you?' Then advance to asking age."
                    if profile.get("name") else
                    "NOW ask their name. SAY EXACTLY: "
                    "'Nice choice 👍 May I know your name?' "
                    "Do NOT ask anything else. JUST ask name."
                )
            ),
            "collect_age":(
                f"STEP 3: User's name is '{name}'. "
                f"Ask age. Say: 'Thanks {name or 'there'}! How old are you? 😊'"
            ),
            "doc_upload":(
                f"STEP 4: Got it{', ' + name if name else ''}! "
                + (f"I've noted your name as '{name}' and age as {profile.get('age','')}. " if name and profile.get('age') else "")
                + "Now ask user to upload their Government ID. The upload widget will appear automatically in the sidebar. "
                "Accepted: Aadhaar, PAN, Driving License, Passport, Voter ID. "
                f"Say something like: 'Great{', ' + name if name else ''}! Please upload your Government ID using the upload section 📎 "
                "You can type \"skip\" if you prefer not to verify right now.' "
                "Keep it warm and brief — 2 sentences max."
            ),
            "verify_wait":(
                "STEP 5: User uploaded document. "
                "Say ONLY: 'Thanks for uploading 😊 I'm checking your document now, "
                "please wait a moment.' Ask NO questions."
            ),
            "collect_gender":(
                f"STEP 6: ID verified. Gender was auto-extracted from the document. "
                f"Profile already has gender='{profile.get('gender','')}'. "
                f"If user just said 'yes details are correct', confirm and ask city: "
                f"'Perfect! Details confirmed ✅ Which city do you live in{', ' + name if name else ''}? 🏙️' "
                f"Otherwise say: 'Great! ✅ Your ID is verified! "
                f"Which city do you live in{', ' + name if name else ''}? 🏙️' "
                "Move directly to asking city — do NOT ask gender manually."
            ),
            "collect_city":(
                f"STEP 7: Ask city. "
                f"Say: 'Which city do you live in{', ' + name if name else ''}? 🏙️'"
            ),
            # ── NEW stages 8-10 ──────────────────────────────────────────────
            "collect_coverage":(
                f"STEP 8: Ask who the policy should cover{', ' + name if name else ''}. "
                "Say: 'Who would you like the insurance policy to cover? 😊' "
                "Radio buttons appear: Myself only / My spouse and children / Whole family."
            ),
            "collect_family_count":(
                f"STEP 9a: User wants family coverage ({profile.get('coverage_type','')}). "
                f"Ask: 'How many family members should be covered? 👨‍👩‍👧‍👦 (Please enter a number)'"
            ),
            "collect_family_medical":(
                f"STEP 9b: Got {profile.get('family_member_count','')} family member(s). "
                "Ask ONE simple question only: 'Does anyone in your family have any existing medical conditions? 🏥' "
                "Radio buttons: No, everyone is healthy / Yes, there are medical conditions. "
                "IMPORTANT: Do NOT ask each member individually. Do NOT ask for documents from family members."
            ),
            "collect_family_members":(
                "STEP 9b (legacy): Ask family member details briefly. Example: Spouse, 40 😊"
            ),
            "collect_family":(
                "STEP 8 (legacy): Ask family members to cover. "
                "Say: 'Who would you like to include in your insurance coverage? 👨‍👩‍👧‍👦' "
                "Multi-select buttons appear automatically."
            ),
            # ── Medical status gate ──────────────────────────────────────────
            "collect_medical_status":(
                f"STEP 10: Ask {name or 'the user'} about their own medical conditions. "
                "Say: 'Do you have any existing medical conditions? 🏥' "
                "Radio buttons: No existing medical conditions / Yes, there are medical conditions. "
                "NOTE: This is for the main user only, NOT family members (already asked separately)."
            ),
            "collect_medical":(
                (
                    "STEP 11: User said YES to medical conditions. Ask for details. "
                    f"Say: 'Please briefly mention the medical condition(s) — for example: Diabetes, Heart condition, High blood pressure. "
                    "(Select all that apply)' "
                    "Buttons appear automatically."
                ) if (profile.get("medical_conditions_status") == "HasConditions"
                      or not profile.get("medical_conditions_status")) else (
                    "STEP 11: Ask about medical conditions (multi-select). "
                    "Buttons appear automatically."
                )
            ),
            # ── Optional medical report (main user only) ──────────────────────
            "optional_medical_report":(
                f"STEP 12: Ask {name or 'the user'} to optionally upload their personal health report. "
                "Say: 'Since you have no medical conditions, uploading a recent health report will help us "
                f"recommend better plans for you{', ' + name if name else ''} 📋 "
                "You can upload it now or skip this step.' "
                "Options: Upload medical report / Skip. Upload widget is on the left. "
                "IMPORTANT: This is for the MAIN USER only — do NOT ask family members to upload documents."
            ),
            # ── Condition-based branches ────────────────────────────────
            "condition_report_upload":(
                f"STEP 9a (Health condition found: {cond}): "
                "The upload widget will appear automatically. Ask user to upload their health/medical report. "
                f"Say: 'Since you have {cond}, please upload your latest health report or "
                "prescription so I can recommend the best plan for your condition 🏥 "
                "You can type \"skip\" if you don't have it handy.' "
                "Upload widget is already visible on the left."
            ),
            "condition_report_wait":(
                "STEP 9b: User uploaded health report. "
                "Say ONLY: 'Thanks! 😊 I'm analyzing your health report now, please wait.' "
                "Ask NO questions."
            ),
            "optional_health_check":(
                f"STEP 9c: User has no conditions. "
                "Ask OPTIONALLY if they want to upload a general health check report. "
                "Say: 'Great, no conditions! 😊 Do you have a recent health check report? "
                "Uploading it can help get better plan rates. "
                "You can also skip this step.' "
                "Upload widget is visible on the left."
            ),
            "vehicle_history":(
                f"STEP 9d: Vehicle insurance for {name or 'user'}. "
                f"We have: name='{name}', city='{profile.get('city','')}', family='{profile.get('family_members','')}'. "
                "Now ask about vehicle history. "
                f"Say: 'Great {name or ''}! Do you have any previous vehicle insurance policy or accident claim history? 🚗' "
                "Radio buttons will appear (Previous Policy / Accident Claim / None)."
            ),
            "vehicle_doc_upload":(
                f"STEP 9e: User has vehicle history. "
                "Ask to upload previous policy or accident claim document. "
                "Say: 'Please upload your previous policy or accident claim document using "
                "the upload section 📄 You can type \"skip\" if unavailable.' "
            ),
            "life_docs":(
                f"STEP 9f: Life/Term insurance. "
                "Ask: 'Do you have any medical history or income documents to share? 📋' "
                "Say these help in getting better term rates. "
                "Upload widget is visible. They can skip."
            ),
            "travel_declare":(
                f"STEP 9g: Travel insurance. "
                "Ask: 'Any health conditions or trip-related issues to declare? ✈️' "
                "Radio buttons appear. They can select None to skip."
            ),
            "property_history":(
                f"STEP 9h: Property insurance for {name or 'user'}. "
                f"We have: name='{name}', city='{profile.get('city','')}'. "
                "Now ask about property history. "
                f"Say: 'Almost there {name or ''}! Any previous damage or insurance claim history for your property? 🏠' "
                "Radio buttons appear (Fire Damage / Flood Damage / Previous Claim / None)."
            ),
            # ── Steps 10-15 ───────────────────────────────────────────────
            "collect_budget":(
                f"STEP 10: Ask monthly budget{', ' + name if name else ''}. "
                "Say: 'Almost there! 🎯 What is your monthly budget for insurance premiums? 💰' "
                "Budget radio buttons appear automatically."
            ),
            # ── Review Details ────────────────────────────────────────────
            "review_details":(
                (lambda: (
                    lambda p: (
                        "STEP 11: CRITICAL — Do NOT say thanks for budget. Do NOT say 'let me check'. "
                        "Do NOT say 'ready to proceed'. The profile is already complete. "
                        "IMMEDIATELY start with EXACTLY this line: '📋 Here are your details — please review before we generate recommendations:' "
                        "Then list ALL fields on separate lines with emojis. "
                        "End with: 'Would you like to continue? 😊' "
                        "Then show this summary in a clean, readable format using emojis:\n"
                        + f"👤 Name: {p.get('name','—')}\n"
                        + f"🎂 Age: {p.get('age','—')}\n"
                        + (f"⚧ Gender: {p.get('gender','—')}\n" if p.get('gender') else "")
                        + f"📍 Location: {p.get('city','—')}\n"
                        + f"🏥 Insurance Type: {p.get('insurance_type','—')}\n"
                        + f"👨‍👩‍👧 Coverage: {p.get('coverage_type','—')}\n"
                        + (
                            f"👨‍👩‍👧‍👦 Family Members: {p.get('family_member_count',0)} member(s)\n"
                            if (p.get('coverage_type','') or '').lower() not in ('','myself only')
                            else ""
                        )
                        + (
                            f"🏥 Family Health: {p.get('family_medical_conditions','—')}\n"
                            if p.get('family_medical_conditions') and
                               (p.get('family_medical_conditions','') or '').lower() != 'none'
                            else ""
                        )
                        + (
                            f"🩺 Your Medical Conditions: {p.get('medical_conditions','None')}\n"
                        )
                        + (
                            f"📄 Medical Report Summary: {p.get('medical_report_summary','—')}\n"
                            if p.get('medical_report_summary') else ""
                        )
                        + f"💰 Budget: {p.get('budget_range','—')}\n"
                        + "\nEnd with: 'Would you like to continue with these details? 😊' "
                        + "Radio buttons: ✅ Continue / ✏️ Change Details"
                    )
                )(profile))()
            ),
            "edit_details":(
                "STEP 11b: User wants to update their details. "
                "Ask warmly: 'Sure! Which details would you like to update? 😊 "
                "You can change: Location, Coverage type, Medical conditions, or Budget.' "
                "Edit buttons appear automatically. "
                "After user updates, confirm the change warmly and say they can continue when ready. "
                "Do NOT ask for verification docs again."
            ),
            "recommendation":(
                (
                    # Plans already shown — do NOT repeat the list
                    "STEP 13: Plans were ALREADY shown to this user. "
                    "DO NOT list plans again. "
                    "User said: '" + message + "'. "
                    "If they said 'none', 'ok thanks', 'no' → ask warmly: "
                    "'Would you like to apply for one of the plans I showed, "
                    "speak to a human advisor, or would you like to change your budget? 😊' "
                    "If they asked a question about a plan → answer it briefly. "
                    "Radio buttons with plan names remain visible."
                ) if profile.get("_plans_already_shown") else (
                    "STEP 13: Recommend exactly 2-3 insurance plans from the Policy Knowledge Base. "
                    "KB recommendations are shown ABOVE this prompt — use them as the PRIMARY source. "
                    + (
                        f"\n🔮 Estimated Premium Range: {profile.get('premium_prediction','')} — "
                        "mention this estimate naturally in your recommendation. "
                        if profile.get("premium_prediction") else ""
                    )
                    + (
                        f"\n📊 Risk Category: {profile.get('risk_category','')} — "
                        "tailor plan suggestions to this risk level. "
                        if profile.get("risk_category") else ""
                    )
                    + "For each plan clearly state: name, company, premium, coverage, waiting period, "
                    "why it specifically suits THIS user's age/budget/medical conditions/city. "
                    + (
                        f"\nFAMILY CONDITIONS: {profile.get('family_medical_conditions') or ''} — "
                        "PRIORITISE family floater plans or plans with family pre-existing disease cover. "
                        if (profile.get("family_medical_conditions") or "").lower() not in ("","none")
                        else ""
                    )
                    + (
                        f"\nMAIN USER CONDITIONS: {profile.get('medical_conditions') or ''} — "
                        "PRIORITISE plans that cover pre-existing diseases with minimal waiting period. "
                        if (profile.get("medical_conditions") or "").lower() not in ("","none")
                        else "\nNo conditions detected — recommend standard health plans with broad coverage. "
                    )
                    + "End with: 'Which plan would you like to know more about?' "
                    "Radio buttons with plan names appear automatically. "
                    "RAG CONTEXT:\n" + (rag_ctx or "(Use built-in knowledge for Indian insurance)")
                )
            ),
            "explain_plan":(
                "STEP 12: User selected a specific plan. Explain it in detail: "
                "coverage amount, monthly premium, waiting period, network hospitals, "
                "key benefits, why this plan is perfect for this user specifically. "
                "Keep it friendly and clear (3-5 sentences). "
                "ALWAYS end with exactly: 'Would you like to apply now or compare other plans?' "
                "Buttons will appear automatically."
            ),
            "ask_escalation":(
                "STEP 13: Ask if they want a human advisor. "
                "Radio buttons will appear. Be warm and friendly."
            ),
            "ask_rating":(
                f"STEP 14: Thank {name or 'the user'} warmly. Ask for 1-5 star rating. "
                "Star rating UI will appear."
            ),
            "farewell":(
                f"STEP 15: Warm personal farewell to {name or 'the user'}. "
                "Include 🎉. Mention documents deleted securely. "
                f"End with: '🎉 Thank you {name or ''}! Your insurance journey is complete.'"
            ),
        }.get(stage, "Continue the conversation following the PolicyBot flow.")

        lang_rule = f"CRITICAL: Reply ONLY in {language}. Do NOT use any other language regardless of what the user wrote."
        # ── Build skip-awareness note for AI ───────────────────────────────
        _skip_note = ""
        _known = [k for k, skip in _mem_should_skip.items() if skip]
        if _known:
            _skip_note = f"\nALREADY KNOWN (DO NOT ask again): {', '.join(_known)}"
        return f"""CURRENT STEP: {stage}
LANGUAGE: {language}
USER NAME: {name or '(not yet provided)'}
INSURANCE TYPE: {ins}
{_skip_note}

SESSION PROFILE:
{json.dumps(safe_p, indent=2)}

{_mem_ctx}

RECENT CONVERSATION:
{recent or '(start of conversation)'}

USER MESSAGE: "{message}"

YOUR TASK:
{instructions}

{lang_rule}
Reply as PolicyBot (warm, 2-3 sentences, correct step only):"""

    # ── Helpers ────────────────────────────────────────────────────────────
    def _confidence(self, profile, stage):
        if stage not in ("recommendation","explain_plan","ask_escalation","ask_rating","farewell"):
            return ""
        return ("High ✅ — Identity verified. Faster approval guaranteed."
                if profile.get("gov_id_verified")
                else "Low ⚠️ — Verify your ID for better rates & faster approval.")

    def _rag_query(self, profile):
        # Include both main user and family conditions for RAG matching
        fam_cond = profile.get("family_medical_conditions","")
        fam_tag  = ("family conditions " + fam_cond) if fam_cond and fam_cond.lower() != "none" else ""
        return " ".join(filter(None,[
            profile.get("insurance_type",""),
            profile.get("medical_conditions",""),
            fam_tag,
            profile.get("coverage_type",""),
            profile.get("family_members",""),
            profile.get("budget_range",""),
            profile.get("city",""),
        ]))

    def _extract_rating(self, message):
        nums = re.findall(r'\b([1-5])\b', message)
        return int(nums[0]) if nums else None

    def _detect_plan(self, message):
        msg = message.lower()
        for k in ["star health","hdfc ergo","niva bupa","care","aditya birla","bajaj allianz",
                  "lic","icici lombard","plan 1","plan 2","plan 3","option 1","option 2"]:
            if k in msg:
                return message.strip()
        return None

    def _module(self, stage):
        m = {
            "insurance_type":"welcome","collect_name":"onboarding","collect_age":"onboarding",
            "doc_upload":"verification","verify_wait":"verification",
            "collect_gender":"profiling","collect_city":"profiling",
            "collect_coverage":"profiling",
            "collect_family_count":"profiling",
            "collect_family_medical":"profiling",  # NEW
            "collect_family_members":"profiling",  # legacy
            "collect_family":"profiling",
            "collect_medical_status":"profiling",    # NEW
            "collect_medical":"profiling",
            "optional_medical_report":"condition_check",  # NEW
            "condition_report_upload":"condition_check","condition_report_wait":"condition_check",
            "optional_health_check":"condition_check",
            "vehicle_history":"condition_check","vehicle_doc_upload":"condition_check",
            "life_docs":"condition_check","travel_declare":"condition_check",
            "property_history":"condition_check",
            "collect_budget":"profiling",
            "review_details":"review",
            "edit_details":"review",
            "fraud_check":"analysis",
            "risk_scoring":"analysis",
            "recommendation":"recommendation",
            "explain_plan":"recommendation","ask_escalation":"escalation",
            "ask_rating":"feedback","farewell":"feedback",
        }
        return m.get(stage, "general")
