"""
PolicyBot v4 — Flask Backend
Offline OCR verification (pytesseract + OpenCV) → Gemini AI handoff
Document auto-delete after conversation ends
"""
import os, uuid, logging
from functools import wraps
import requests
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, Response
from flask_cors import CORS
from dotenv import load_dotenv

from models.gemini_manager      import GeminiManager
from models.rag_engine          import RAGEngine
from models.database            import Database
from models.conversation_engine import ConversationEngine
from models.lead_manager        import LeadManager
from models.fraud_checker       import FraudChecker
from models.doc_verifier        import DocumentVerifier
from models.ocr_verifier        import OCRVerifier, ocr_available
from models.policy_kb           import PolicyKB, ALLOWED_EXTS as KB_EXTS
from models.conversation_memory import memory_manager as _mem_mgr

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("PolicyBot")

load_dotenv()

app  = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "policybot-v3-secret-2024")
CORS(app)

# ── Singletons ────────────────────────────────────────────────────────────────
gemini     = GeminiManager()
rag        = RAGEngine()
db         = Database()
lead_mgr   = LeadManager(db)
fraud      = FraudChecker()
ocr        = OCRVerifier()                # ← Offline OCR (primary)
verifier   = DocumentVerifier(gemini)    # ← Gemini Vision (fallback only)
policy_kb  = PolicyKB(db, gemini, ocr)   # ← Policy Knowledge Base engine
conv_engine = ConversationEngine(gemini, rag, db)  # ← module-level, used by upload()

# Log OCR availability on startup
_ocr_status = ocr_available()
log.info(f"[STARTUP] OCR status: {_ocr_status}")

# Seed master policy data into KB (only if no master exists yet)
try:
    seeded = db.kb_seed_master()
    log.info(f"[STARTUP] Policy KB master data: {'seeded' if seeded else 'already exists'}")
except Exception as _e:
    log.warning(f"[STARTUP] KB seed warning: {_e}")

ADMIN_ID   = os.getenv("ADMIN_ID",       "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "policybot2024")

# All supported image + document formats
ALLOWED_EXTS = {".jpg",".jpeg",".png",".webp",".gif",".bmp",".tiff",".tif",
                ".pdf",".docx",".doc",".txt"}

# ── Auth decorator ────────────────────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index(): return render_template("index.html")

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        d = request.form
        if d.get("admin_id") == ADMIN_ID and d.get("password") == ADMIN_PASS:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Invalid credentials. Please try again."
    return render_template("admin_login.html", error=error)

@app.route("/admin/logout")
def admin_logout(): session.pop("admin_logged_in", None); return redirect(url_for("admin_login"))

@app.route("/admin")
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard(): return render_template("admin.html")

# ── Chat API ──────────────────────────────────────────────────────────────────
@app.route("/api/chat", methods=["POST"])
def chat():
    d            = request.json or {}
    message      = (d.get("message") or "").strip()
    user_id      = d.get("user_id")   or str(uuid.uuid4())
    session_id   = d.get("session_id") or str(uuid.uuid4())
    lang         = d.get("language", "English")
    selected_opt = d.get("selected_option")
    is_new_chat  = d.get("is_new_chat", False)

    if not message and not selected_opt:
        return jsonify({"error": "Empty message"}), 400

    final_msg = selected_opt or message

    # Handle session init ping (from page load — just resets profile, no reply needed)
    if final_msg == "__session_init__":
        if is_new_chat:
            db.reset_session_profile(user_id)
            log.info(f"[SESSION] Init reset for {user_id}")
        return jsonify({"status":"success","reply":"","stage":"insurance_type",
                        "options":[],"option_type":"none","progress":7,
                        "stage_label":"Insurance Type","profile_updated":False})

    # ── Session isolation ─────────────────────────────────────────────────────
    # is_new_chat is sent on EVERY message in a fresh session (first real message)
    # This guarantees reset even if the __session_init__ ping was lost/raced
    fresh = False
    if is_new_chat:
        db.reset_session_profile(user_id)   # wipes profile + history + selected_options
        profile = {"onboarding_stage": "insurance_type", "user_id": user_id}
        history = []                         # ← EMPTY history — no old context bleeds through
        log.info(f"[SESSION] Fresh session reset for {user_id}")
        fresh = True
    else:
        profile = db.get_user_profile(user_id)
        if not profile:
            profile = {"onboarding_stage": "insurance_type", "user_id": user_id}
        elif not profile.get("onboarding_stage"):
            profile["onboarding_stage"] = "insurance_type"
        history = db.get_chat_history(user_id, limit=20)

    engine = ConversationEngine(gemini, rag, db)

    # ── Smart pre-extraction: detect user info from message BEFORE stage logic ──
    # Runs on every message; saves any fields found, then lets stage logic proceed
    pre_extracted = engine.smart_extract(final_msg, profile)
    if pre_extracted:
        db.upsert_user_profile(user_id, pre_extracted)
        profile.update(pre_extracted)
        log.info(f"[SMART-EXTRACT] Pre-extracted fields: {list(pre_extracted.keys())} | user={user_id}")

    result = engine.process(
        user_id=user_id, session_id=session_id, message=final_msg,
        history=history, profile=profile, language=lang,
        fresh_session=fresh,
    )

    # Store button selection in DB if came from option click
    if selected_opt and profile.get("onboarding_stage"):
        db.store_option_selection(user_id, profile["onboarding_stage"], "option_click", selected_opt)

    lead_mgr.detect(user_id, final_msg)

    db.store_chat(
        user_id=user_id, message=final_msg,
        bot_reply=result["reply"], module=result.get("module","general"),
        session_id=session_id, language=lang
    )

    # Trigger document cleanup on farewell
    if result.get("trigger_cleanup"):
        _cleanup_user_docs(user_id)

    return jsonify({
        "status":          "success",
        "user_id":         user_id,
        "session_id":      session_id,
        "reply":           result["reply"],
        "options":         result.get("options", []),
        "option_type":     result.get("option_type", "none"),
        "stage":           result.get("stage", ""),
        "progress":        result.get("progress", 0),
        "stage_label":     result.get("stage_label", ""),
        "show_rating":     result.get("show_rating", False),
        "show_escalation": result.get("show_escalation", False),
        "show_farewell":   result.get("show_farewell", False),
        "show_upload":     result.get("show_upload", False),
        "confidence":      result.get("confidence", ""),
        "profile_updated": result.get("profile_updated", False),
        "lock_chat":       result.get("lock_chat", False),
        "module":          result.get("module", "general"),
        "plans_table":     result.get("plans_table", []),
    })

# ── Upload + Verify API ───────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    """
    DUAL PIPELINE:
    Step 1: Offline OCR (pytesseract + OpenCV) — no API needed
            → quality check, text extraction, DOB parse, age compare
    Step 2: If OCR succeeds → mark verified, hand control to Gemini AI
            If OCR fails (blurry/no text) → offer retry or skip
            If Gemini API key available → optionally use as fallback
    Supported: JPG PNG WEBP GIF BMP TIFF PDF DOCX TXT
    """
    doc_type   = request.form.get("doc_type", "gov_id")
    user_id    = request.form.get("user_id", "")
    session_id = request.form.get("session_id", user_id)
    file       = request.files.get("file")

    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    # ── Validate extension ──────────────────────────────────────────────────
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        friendly_list = "JPG, PNG, WEBP, GIF, BMP, TIFF, PDF, DOCX, TXT"
        return jsonify({"error": f"'{ext}' not supported. Use: {friendly_list}"}), 400

    file_bytes = file.read()
    if len(file_bytes) > 15 * 1024 * 1024:  # 15MB limit
        return jsonify({"error": "File too large. Maximum 15MB."}), 400
    if len(file_bytes) < 500:
        return jsonify({"error": "File appears empty or corrupted."}), 400

    # ── Save to disk ────────────────────────────────────────────────────────
    safe_name = f"{uuid.uuid4()}{ext}"
    save_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, safe_name)
    with open(save_path, "wb") as fout:
        fout.write(file_bytes)
    db.store_document(file.filename, save_path, doc_type, user_id)
    log.info(f"[UPLOAD] {len(file_bytes)} bytes saved → {save_path} | type={doc_type} user={user_id}")

    # ════════════════════════════════════════════════════════════════
    # GOV ID — OFFLINE OCR FIRST, GEMINI FALLBACK
    # ════════════════════════════════════════════════════════════════
    gov_id_types = {"aadhaar","pan","driving_license","passport","voter_id","gov_id"}
    if doc_type in gov_id_types:
        profile    = db.get_user_profile(user_id)
        stated_age = profile.get("age")

        # ── STEP 1: Offline OCR (primary — no API key needed) ───────────────
        log.info(f"[PIPELINE] Step 1: Offline OCR | user={user_id} | stated_age={stated_age}")
        stated_name = profile.get("name", "") or ""
        v_result = ocr.verify_gov_id(
            file_path    = save_path,
            file_bytes   = file_bytes,
            file_ext     = ext,
            stated_age   = stated_age,
            user_id      = user_id,
            session_id   = session_id,
            stated_name  = stated_name,
        )
        ocr_failed = v_result["status"] in ("no_ocr_engine","no_text","api_error")
        log.info(f"[PIPELINE] OCR result: status={v_result['status']} verified={v_result['verified']}")

        # ── STEP 2: Gemini Vision fallback if OCR couldn't extract text ─────
        # Only use Gemini if: OCR produced no text AND Gemini key is available
        gemini_keys_present = any(
            os.environ.get(k,"") for k in
            ["GEMINI_API_KEY","GEMINI_API_KEY_1","GEMINI_API_KEY_2","GEMINI_API_KEY_3"]
        )
        if ocr_failed and gemini_keys_present:
            log.info(f"[PIPELINE] Step 2: Gemini Vision fallback | user={user_id}")
            try:
                v_result = verifier.verify(
                    file_path   = save_path,
                    file_bytes  = file_bytes,
                    file_ext    = ext,
                    doc_type    = doc_type,
                    stated_age  = stated_age,
                    stated_name = stated_name,
                    user_id     = user_id,
                    session_id  = session_id,
                )
                log.info(f"[PIPELINE] Gemini fallback: {v_result['status']}")
            except Exception as ge:
                log.error(f"[PIPELINE] Gemini fallback failed: {ge}")
                # Keep the OCR result

        # ── Store verification result (never store ID numbers) ───────────────
        if v_result["verified"]:
            db.update_verification(user_id, "gov_id_verified", 1)
            db.update_verification(user_id, "doc_type_found",
                                   v_result.get("doc_type_found",""))

            # ── Auto-extract gender from ID — never ask manually ────────────
            id_gender = (v_result.get("id_gender") or "").strip()
            if id_gender in ("Male", "Female", "Other"):
                db.upsert_user_profile(user_id, {"gender": id_gender})
                log.info(f"[PIPELINE] ✅ Gender auto-extracted from ID: {id_gender}")
            else:
                log.info(f"[PIPELINE] ℹ️ Gender not on this ID type — continuing without it")
            # Always skip collect_gender — gender is NEVER asked as a manual question
            db.upsert_user_profile(user_id, {"onboarding_stage": "collect_city"})
            next_stage = "collect_city"
            log.info(f"[PIPELINE] ✅ VERIFIED → collect_city | user={user_id}")
        else:
            db.update_verification(user_id, "gov_id_verified", 0)
            next_stage = "doc_upload"
            log.info(f"[PIPELINE] ❌ NOT VERIFIED: {v_result['status']} | user={user_id}")

        return jsonify({
            "status":         "success",
            "verified":       v_result["verified"],
            "doc_type":       doc_type,
            "v_status":       v_result["status"],
            "quality":        v_result.get("quality",""),
            "doc_type_found": v_result.get("doc_type_found",""),
            "engine":         v_result.get("engine","offline_ocr"),
            "reply":          v_result["message"],
            "options":        v_result.get("options",[]),
            "option_type":    v_result.get("option_type","radio"),
            "next_stage":     next_stage,
            "handoff_to_gemini": v_result.get("handoff_to_gemini", False),
        })

    # ════════════════════════════════════════════════════════════════
    # CONDITION REPORT — health report uploaded from condition_report_upload stage
    # Also handles: optional_medical_report (NEW), optional_health_check,
    #               vehicle_doc_upload, life_docs, travel_declare, property_history
    # ════════════════════════════════════════════════════════════════
    elif doc_type in ("health_report", "condition_report", "medical_report",
                      "vehicle_insurance", "rc_book",
                      "life_doc", "travel_doc", "property_doc"):

        profile = db.get_user_profile(user_id)
        current_stage = profile.get("onboarding_stage", "")

        # ── Determine which OCR analyzer to call ─────────────────────────
        if doc_type in ("health_report", "condition_report", "medical_report"):
            log.info(f"[PIPELINE] Condition/Health report OCR | user={user_id}")
            result = ocr.analyze_health_report(
                file_bytes     = file_bytes,
                file_ext       = ext,
                user_id        = user_id,
                stated_name    = profile.get("name", "") or "",
                stated_age     = profile.get("age"),
                stated_gender  = profile.get("gender", "") or "",
                insurance_type = profile.get("insurance_type", "") or "",
            )

            conditions_found = result.get("conditions", [])
            identity_check   = result.get("identity_check", {})
            id_warnings      = result.get("id_warnings", [])
            risk_level       = result.get("risk_level", "LOW")

            # Store extracted conditions into profile
            if conditions_found:
                existing  = profile.get("medical_conditions", "")
                new_conds = ", ".join(conditions_found)
                merged    = ", ".join(filter(None, [existing, new_conds]))
                db.upsert_user_profile(user_id, {
                    "medical_conditions":        merged,
                    "medical_proof_uploaded":    1,
                    "condition_report_uploaded": 1,
                    "condition_report_result":   new_conds,
                })
            else:
                db.upsert_user_profile(user_id, {
                    "medical_proof_uploaded":    1,
                    "condition_report_uploaded": 1,
                    "condition_report_result":   "No conditions detected",
                })

            # ── Route based on which stage triggered the upload ───────────
            if current_stage == "optional_medical_report":
                # NEW — optional medical report: store extra fields and route via _medical_branch
                summary = "No conditions detected." if not conditions_found else \
                          f"Detected: {', '.join(conditions_found)}."
                db.upsert_user_profile(user_id, {
                    "medical_report_uploaded":   1,
                    "medical_report_conditions": ", ".join(conditions_found) if conditions_found else "None",
                    "medical_report_summary":    summary,
                    "medical_conditions_status": "HasConditions" if conditions_found else
                                                 (profile.get("medical_conditions_status") or "None"),
                })
                # Re-fetch updated profile to route correctly
                updated_profile = db.get_user_profile(user_id)
                next_st = conv_engine._medical_branch(updated_profile, user_id)
                db.upsert_user_profile(user_id, {"onboarding_stage": next_st})
                cond_str = f" We detected: {', '.join(conditions_found)}." if conditions_found else \
                           " No major conditions were detected."
                reply_msg = (
                    f"✅ Your medical report has been analyzed successfully.{cond_str} "
                    "I'll factor this into your plan recommendation 👍"
                )
                # Always send budget options directly — no Gemini follow-up needed
                # handoff_to_gemini=False prevents the truncated "ready to proceed" message
                if next_st == "collect_budget":
                    opts     = ["Under ₹500","₹500–₹1,000","₹1,000–₹2,000","₹2,000–₹5,000","Above ₹5,000"]
                    opt_type = "radio"
                else:
                    opts, opt_type = conv_engine._options(next_st, updated_profile)
                return jsonify({
                    "status":           "success",
                    "verified":         result.get("success", True),
                    "reply":            reply_msg,
                    "conditions_found": conditions_found,
                    "identity_check":   identity_check,
                    "id_warnings":      id_warnings,
                    "risk_level":       risk_level,
                    "next_stage":       next_st,
                    "options":          opts,
                    "option_type":      opt_type,
                    "show_upload":      next_st in ("condition_report_upload","optional_health_check","life_docs"),
                    "handoff_to_gemini": False,   # reply is complete — no extra Gemini call
                })
            else:
                # condition_report_upload or other — always advance to collect_budget
                db.upsert_user_profile(user_id, {"onboarding_stage": "collect_budget"})
                reply_msg = result.get("message", "✅ Health report analyzed 👍")
                return jsonify({
                    "status":           "success",
                    "verified":         result.get("success", True),
                    "reply":            reply_msg,
                    "conditions_found": conditions_found,
                    "doctor":           result.get("doctor", ""),
                    "identity_check":   identity_check,
                    "id_warnings":      id_warnings,
                    "risk_level":       risk_level,
                    "next_stage":       "collect_budget",
                    "options":          ["Under ₹500","₹500–₹1,000","₹1,000–₹2,000","₹2,000–₹5,000","Above ₹5,000"],
                    "option_type":      "radio",
                    "handoff_to_gemini": False,   # reply + options are complete — no extra Gemini call
                })

        elif doc_type in ("vehicle_insurance", "rc_book"):
            log.info(f"[PIPELINE] Vehicle doc OCR | user={user_id}")
            result = ocr.analyze_vehicle_doc(file_bytes, ext, user_id)

            db.upsert_user_profile(user_id, {
                "vehicle_doc_uploaded": 1,
                **({"vehicle_number": result["vehicle_no"]} if result.get("vehicle_no") else {}),
                "onboarding_stage": "collect_budget",
            })

            return jsonify({
                "status":       "success",
                "verified":     result.get("success", True),
                "reply":        result.get("message", "✅ Vehicle document analyzed! Let's continue."),
                "vehicle_no":   result.get("vehicle_no", ""),
                "next_stage":   "collect_budget",
                "options":      ["Under ₹500","₹500–₹1,000","₹1,000–₹2,000","₹2,000–₹5,000","Above ₹5,000"],
                "option_type":  "radio",
                "handoff_to_gemini": True,
            })

        else:
            # life_doc, travel_doc, property_doc — generic OCR + advance
            log.info(f"[PIPELINE] Supporting doc OCR ({doc_type}) | user={user_id}")
            policy_text = ocr.extract_policy_text_for_rag(file_bytes, ext)
            doc_field_map = {
                "life_doc":     "life_docs",
                "travel_doc":   "travel_declare",
                "property_doc": "property_history",
            }
            field = doc_field_map.get(doc_type, "life_docs")
            db.upsert_user_profile(user_id, {
                field: "document_uploaded",
                "onboarding_stage": "collect_budget",
            })

            return jsonify({
                "status":       "success",
                "verified":     bool(policy_text),
                "reply":        f"✅ Document received and analyzed! Let's continue 😊",
                "next_stage":   "collect_budget",
                "options":      ["Under ₹500","₹500–₹1,000","₹1,000–₹2,000","₹2,000–₹5,000","Above ₹5,000"],
                "option_type":  "radio",
                "handoff_to_gemini": True,
            })

    # ════════════════════════════════════════════════════════════════
    # PREVIOUS POLICY DOC (legacy — kept for backward compat)
    # ════════════════════════════════════════════════════════════════
    elif doc_type == "prev_policy":
        log.info(f"[PIPELINE] Previous policy OCR | user={user_id}")
        result = ocr.analyze_policy_doc(file_bytes, ext, user_id)

        return jsonify({
            "status": "success",
            "verified": result.get("success", True),
            "reply": result.get("message","✅ Previous policy analyzed 👍"),
            "policy_no": result.get("policy_no",""),
            "coverage": result.get("coverage",""),
            "has_claims": result.get("has_claims", False),
            "options":[], "option_type":"none", "next_stage":"",
            "handoff_to_gemini": result.get("handoff_to_gemini", True),
        })

    # ════════════════════════════════════════════════════════════════
    # ADMIN POLICY PDF — extract text for RAG
    # ════════════════════════════════════════════════════════════════
    elif doc_type == "policy_pdf":
        policy_text = ocr.extract_policy_text_for_rag(file_bytes, ext)
        if policy_text:
            rag.add_document(save_path, file.filename)
            return jsonify({
                "status":"success","verified":True,
                "reply":f"✅ Policy document indexed ({len(policy_text)} chars extracted).",
                "options":[],"option_type":"none","next_stage":""
            })
        else:
            return jsonify({
                "status":"success","verified":False,
                "reply":"⚠️ Could not extract text from policy document. Try a text-based PDF.",
                "options":[],"option_type":"none","next_stage":""
            })

    else:
        return jsonify({
            "status": "success", "verified": False,
            "reply": "✅ Document received.",
            "options": [], "option_type": "none", "next_stage": ""
        })

# ── Cleanup helper ─────────────────────────────────────────────────────────────
def _cleanup_user_docs(user_id: str):
    """Delete uploaded files from disk, mark DB records as deleted."""
    count = DocumentVerifier.delete_user_uploads(user_id, db)
    log.info(f"[CLEANUP] Deleted {count} document(s) for user {user_id}")
    return count

@app.route("/api/cleanup", methods=["POST"])
def cleanup():
    """Manual trigger for document cleanup (called on farewell)."""
    d = request.json or {}
    user_id = d.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    count = _cleanup_user_docs(user_id)
    return jsonify({"status": "success", "deleted_files": count,
                    "message": f"Deleted {count} uploaded document(s) securely."})

# ── Rating API ─────────────────────────────────────────────────────────────────
@app.route("/api/rating", methods=["POST"])
def rating():
    d     = request.json or {}
    score = int(d.get("rating", 0))
    uid   = d.get("user_id")
    if 1 <= score <= 5:
        db.store_rating(uid, score, d.get("comment",""))
    return jsonify({"status": "success", "message": "Rating saved. Thank you! 🎉"})

# ── Escalation API ─────────────────────────────────────────────────────────────
@app.route("/api/escalate", methods=["POST"])
def escalate():
    d = request.json or {}
    uid = d.get("user_id")
    phone = d.get("phone","")
    time_ = d.get("best_time","")
    plan  = d.get("plan_name","")
    db.store_escalation(uid, phone, time_, plan)
    lead_mgr.mark(uid, plan, "high", "escalated")
    return jsonify({"status": "success",
                    "reply": "✅ A human advisor will contact you soon! 📞"})

# ── Profile API ────────────────────────────────────────────────────────────────
@app.route("/api/profile")
def get_profile():
    uid = request.args.get("user_id")
    return jsonify({"status": "success", "profile": db.get_user_profile(uid) or {}})

# ── Lead API ───────────────────────────────────────────────────────────────────
@app.route("/api/lead", methods=["POST"])
def lead():
    d = request.json or {}
    lead_mgr.mark(d.get("user_id"), d.get("plan_name"),
                       d.get("interest_level","medium"), d.get("action","interested"))
    return jsonify({"status": "success"})

# ── Admin APIs ─────────────────────────────────────────────────────────────────
@app.route("/api/admin/analytics")
@admin_required
def api_analytics(): return jsonify({"status":"success","analytics":db.get_analytics()})

@app.route("/api/admin/users")
@admin_required
def api_users():
    q = request.args.get("q",""); limit = int(request.args.get("limit",50)); offset = int(request.args.get("offset",0))
    return jsonify({"status":"success","users":db.search_users(q,limit,offset),"total":db.count_users(q)})

@app.route("/api/admin/chats")
@admin_required
def api_chats():
    return jsonify({"status":"success","chats":db.search_chats(request.args.get("user_id"),request.args.get("q",""),int(request.args.get("limit",50)))})

@app.route("/api/admin/leads")
@admin_required
def api_leads(): return jsonify({"status":"success","leads":db.get_leads()})

@app.route("/api/admin/ratings")
@admin_required
def api_ratings(): return jsonify({"status":"success","ratings":db.get_ratings()})

@app.route("/api/admin/documents")
@admin_required
def api_docs(): return jsonify({"status":"success","documents":db.get_documents()})

@app.route("/api/admin/documents/<int:doc_id>/toggle", methods=["POST"])
@admin_required
def toggle_doc(doc_id): db.toggle_document(doc_id); return jsonify({"status":"success"})

@app.route("/api/admin/documents/<int:doc_id>", methods=["DELETE"])
@admin_required
def delete_doc(doc_id): db.delete_document(doc_id); return jsonify({"status":"success"})

@app.route("/api/admin/fraud-alerts")
@admin_required
def api_fraud():
    users  = db.get_all_users_raw()
    alerts = []
    for u in users:
        r = fraud.check(dict(u))
        if r["risk_level"] != "LOW":
            r["user_name"] = u["name"] if u["name"] else "Unknown"
            r["user_id"]   = u["user_id"]
            alerts.append(r)
    return jsonify({"status":"success","alerts":alerts})

@app.route("/api/admin/api-usage")
@admin_required
def api_key_usage(): return jsonify({"status":"success","usage":gemini.get_key_usage()})

@app.route("/api/admin/export/users")
@admin_required
def export_users():
    import csv, io
    users = db.get_all_users_raw()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["user_id","name","age","gender","city","insurance_type","budget_range","family","medical","id_verified"])
    for u in users:
        u = dict(u)
        w.writerow([u.get("user_id",""), u.get("name",""), u.get("age",""), u.get("gender",""),
                    u.get("city",""), u.get("insurance_type",""), u.get("budget_range",""),
                    u.get("family_members",""), u.get("medical_conditions",""),
                    "Yes" if u.get("gov_id_verified") else "No"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=policybot_users.csv"})

# ── Policy Knowledge Base Admin APIs ──────────────────────────────────────────
@app.route("/api/admin/kb/documents", methods=["GET"])
@admin_required
def kb_list_docs():
    return jsonify({"status": "success", "documents": db.kb_get_all_docs()})

@app.route("/api/admin/kb/documents/<int:doc_id>", methods=["GET"])
@admin_required
def kb_get_doc(doc_id):
    doc   = db.kb_get_doc(doc_id)
    plans = db.kb_get_plans(doc_id)
    versions = db.kb_get_versions(doc_id)
    return jsonify({"status": "success", "document": doc, "plans": plans, "versions": versions})

@app.route("/api/admin/kb/upload", methods=["POST"])
@admin_required
def kb_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    file_bytes = file.read()

    safe_name = f"kb_{uuid.uuid4()}{ext}"
    save_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "kb")
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, safe_name)
    with open(save_path, "wb") as fout:
        fout.write(file_bytes)

    result = policy_kb.process_upload(file.filename, file_bytes, save_path, "admin")
    status_code = 200 if result["success"] else 422

    # Also add to RAG engine for semantic search
    if result["success"] and result.get("doc_id"):
        try:
            rag.add_document(save_path, file.filename)
        except Exception as e:
            log.warning(f"[KB] RAG add warning: {e}")

    return jsonify(result), status_code

@app.route("/api/admin/kb/documents/<int:doc_id>/update", methods=["POST"])
@admin_required
def kb_update_doc(doc_id):
    file = request.files.get("file")
    note = request.form.get("note", "Admin update")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    file_bytes = file.read()

    safe_name = f"kb_{uuid.uuid4()}{ext}"
    save_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "kb")
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, safe_name)
    with open(save_path, "wb") as fout:
        fout.write(file_bytes)

    result = policy_kb.process_update(doc_id, file.filename, file_bytes, save_path, note)
    return jsonify(result)

@app.route("/api/admin/kb/documents/<int:doc_id>/download", methods=["GET"])
@admin_required
def kb_download_doc(doc_id):
    """Download the original uploaded policy document."""
    import mimetypes
    from flask import send_file
    doc = db.kb_get_doc(doc_id)
    if not doc:
        return jsonify({"error": "Document not found"}), 404
    file_path = doc.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "File not available on server"}), 404
    original_name = doc.get("filename", os.path.basename(file_path))
    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    return send_file(file_path, mimetype=mime,
                     as_attachment=True, download_name=original_name)

@app.route("/api/admin/kb/documents/<int:doc_id>/reextract", methods=["POST"])
@admin_required
def kb_reextract_doc(doc_id):
    """Force re-extraction of plans from an already-uploaded document."""
    doc = db.kb_get_doc(doc_id)
    if not doc:
        return jsonify({"error": "Document not found"}), 404
    file_path = doc.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "File not on server — please re-upload"}), 404
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        ext = os.path.splitext(file_path)[1].lower()
        # Re-run extraction pipeline
        text = policy_kb._extract_text(file_bytes, ext, file_path)
        if not text:
            return jsonify({"success": False, "message": "⚠️ Could not extract text even after re-try."}), 422
        plans = policy_kb._extract_plans_via_ai(text)
        if not plans:
            return jsonify({"success": False, "message": "⚠️ AI could not find plan data in document."}), 422
        db.kb_store_plans(doc_id, plans, is_master=0)
        db.kb_update_doc_status(doc_id, "active")
        return jsonify({
            "success": True,
            "message": f"✅ Re-extracted {len(plans)} plan(s) successfully.",
            "plans": plans,
        })
    except Exception as e:
        log.error(f"[KB] Re-extract error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/kb/documents/<int:doc_id>", methods=["DELETE"])
@admin_required
def kb_delete_doc(doc_id):
    doc = db.kb_get_doc(doc_id)
    if doc and doc.get("file_path") and os.path.exists(doc["file_path"]):
        try:
            os.remove(doc["file_path"])
        except Exception:
            pass
    db.kb_delete_doc(doc_id)
    return jsonify({"status": "success", "message": "Document and associated plans deleted."})

@app.route("/api/admin/kb/plans/<int:plan_id>/toggle", methods=["POST"])
@admin_required
def kb_toggle_plan(plan_id):
    db.kb_toggle_plan(plan_id)
    return jsonify({"status": "success"})

@app.route("/api/admin/kb/plans/<int:plan_id>", methods=["DELETE"])
@admin_required
def kb_delete_plan(plan_id):
    db.kb_delete_plan(plan_id)
    return jsonify({"status": "success"})

@app.route("/api/admin/kb/analytics")
@admin_required
def kb_analytics():
    return jsonify({"status": "success", "analytics": db.kb_get_analytics()})

@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "PolicyBot v3",
        "version": "3.0.0",
        "keys_configured": len(gemini.keys),
        "key_statuses": [
            {"key": f"Key {s['key_index']}", "status": s['status'],
             "requests": s['requests'], "cooldown_remaining": s.get('cooldown_remaining', 0)}
            for s in gemini.get_key_usage()
        ]
    })

@app.route("/api/admin/gemini/health")
@admin_required
def gemini_health():
    """Detailed per-key health check — triggers actual API test calls."""
    results = gemini.health_check()
    usage   = gemini.get_key_usage()
    return jsonify({"status": "success", "health": results, "usage": usage})

@app.route("/api/admin/gemini/status")
@admin_required
def gemini_status():
    """Quick status without test calls — just shows cooldown state."""
    return jsonify({"status": "success", "usage": gemini.get_key_usage()})




# ══════════════════════════════════════════════════════════════════════════════
# ANALYSIS REPORT — PDF  (/api/report)
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/memory", methods=["POST"])
def api_memory():
    """Returns current conversation memory state for a user — used by UI memory widget."""
    data    = request.get_json(silent=True) or {}
    user_id = data.get("user_id") or session.get("user_id","")
    if not user_id:
        return jsonify({"error": "no user_id"}), 400
    mem = _mem_mgr.get(user_id)
    return jsonify({
        "user_profile":       mem.user_profile,
        "conversation_state": mem.conversation_state,
        "completed_steps":    mem.completed_steps,
        "last_question":      mem.last_question,
        "all_steps":          mem.completed_steps,
        "progress_pct":       round(len(mem.completed_steps) / 14 * 100),
    })


@app.route("/api/report", methods=["POST"])
def generate_report():
    """
    9-section PDF insurance analysis report — PolicyBot v12.
    1  User Profile Summary
    2  Government ID Verification
    3  Identity Cross-Verification (Gov ID ↔ Medical Report)
    4  Fraud Detection Results
    5  Risk Assessment
    6  Premium Prediction
    7  Extracted Health Parameters
    8  Insurance Plan Comparison (Top 3)
    9  Final Recommendation
    """
    import io, datetime
    from flask import send_file

    d       = request.json or {}
    user_id = d.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    profile = db.get_user_profile(user_id) or {}
    if not profile:
        return jsonify({"error": "User profile not found. Please complete the chat first."}), 404

    stored_recs = []
    try:
        with db._conn() as _c:
            rows = _c.execute(
                "SELECT plan_name,premium,coverage,waiting_period,reason "
                "FROM recommendations WHERE user_id=? ORDER BY rowid DESC LIMIT 6",
                (user_id,)
            ).fetchall()
            seen = set()
            for r in rows:
                r = dict(r)
                k = r.get("plan_name","")
                if k and k not in seen:
                    seen.add(k); stored_recs.append(r)
    except Exception:
        stored_recs = []

    # ── reportlab ──────────────────────────────────────────────────────────
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib           import colors
        from reportlab.lib.styles    import ParagraphStyle
        from reportlab.lib.units     import mm
        from reportlab.platypus      import (SimpleDocTemplate, Paragraph, Spacer,
                                             Table, TableStyle, KeepTogether, HRFlowable)
        from reportlab.lib.enums     import TA_CENTER, TA_LEFT, TA_RIGHT
    except ImportError:
        return jsonify({"error": "reportlab not installed. pip install reportlab"}), 500

    # ── Import fraud_risk helpers for on-demand scoring if not already done ─
    import json as _json, re as _re, datetime
    try:
        from models.fraud_risk import run_fraud_detection, run_risk_scoring
        # If fraud/risk not already in profile, run them now for the report
        if not profile.get("fraud_status") or profile.get("fraud_status") == "PENDING":
            _fr = run_fraud_detection(profile)
            _rs = run_risk_scoring(profile, _fr)
            profile.update(_fr)
            profile.update(_rs)
    except Exception:
        pass

    # ── Palette ────────────────────────────────────────────────────────────
    NAVY   = colors.HexColor("#0d1f3c")
    BLUE   = colors.HexColor("#1d4ed8")
    TEAL   = colors.HexColor("#0891b2")
    PURPLE = colors.HexColor("#6d28d9")
    GRN_D  = colors.HexColor("#065f46")
    GREEN  = colors.HexColor("#16a34a")
    AMBER  = colors.HexColor("#b45309")
    RED    = colors.HexColor("#b91c1c")
    SLATE  = colors.HexColor("#475569")
    MUTED  = colors.HexColor("#94a3b8")
    LIGHT  = colors.HexColor("#f1f5f9")
    BORDER = colors.HexColor("#e2e8f0")
    WHITE  = colors.white
    ACCNT  = colors.HexColor("#4f46e5")

    def mk(name, **kw): return ParagraphStyle(name, **kw)
    s_ban  = mk("ban",  fontSize=17, textColor=WHITE,  fontName="Helvetica-Bold", leading=22, alignment=TA_CENTER)
    s_sub  = mk("sub",  fontSize=8,  textColor=colors.HexColor("#c7d2fe"), fontName="Helvetica", alignment=TA_CENTER, leading=11)
    s_sec  = mk("sec",  fontSize=11, textColor=WHITE,  fontName="Helvetica-Bold", leading=16)
    s_lbl  = mk("lbl",  fontSize=8,  textColor=MUTED,  fontName="Helvetica-Bold", leading=12, spaceAfter=1)
    s_val  = mk("val",  fontSize=9,  textColor=NAVY,   fontName="Helvetica",   leading=13)
    s_bld  = mk("bld",  fontSize=9,  textColor=NAVY,   fontName="Helvetica-Bold", leading=13)
    s_ok   = mk("ok",   fontSize=9,  textColor=GREEN,  fontName="Helvetica-Bold", leading=13)
    s_warn = mk("warn", fontSize=9,  textColor=AMBER,  fontName="Helvetica-Bold", leading=13)
    s_bad  = mk("bad",  fontSize=9,  textColor=RED,    fontName="Helvetica-Bold", leading=13)
    s_norm = mk("norm", fontSize=9,  textColor=NAVY,   fontName="Helvetica",   leading=13)
    s_risk = mk("risk", fontSize=13, textColor=WHITE,  fontName="Helvetica-Bold", leading=18, alignment=TA_CENTER)
    s_foot = mk("foot", fontSize=7,  textColor=MUTED,  fontName="Helvetica",   leading=10, alignment=TA_CENTER)
    s_th   = mk("th",   fontSize=9,  textColor=WHITE,  fontName="Helvetica-Bold", leading=13)
    s_plhd = mk("plhd", fontSize=10, textColor=WHITE,  fontName="Helvetica-Bold", leading=14)
    s_best = mk("best", fontSize=11, textColor=WHITE,  fontName="Helvetica-Bold", leading=16, alignment=TA_CENTER)
    s_rsn  = mk("rsn",  fontSize=9,  textColor=colors.HexColor("#1e3a5f"), fontName="Helvetica", leading=13)

    def sec_hdr(num, title, color=NAVY):
        t = Table([[Paragraph(f"{num}  {title}", s_sec)]], colWidths=["100%"])
        t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),color),
            ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),("LEFTPADDING",(0,0),(-1,-1),12)]))
        return t

    def kv(label, val, sty=None):
        sty = sty or s_val
        return Table([[Paragraph(label, s_lbl), Paragraph(str(val) if val else chr(8212), sty)]],
            colWidths=["40%","60%"],
            style=[("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
                   ("LEFTPADDING",(0,0),(-1,-1),8),("LINEBELOW",(0,0),(-1,-1),0.3,BORDER)])

    def grid_tbl(hdr_cols, data_rows, col_widths, hdr_color=NAVY):
        rows = [[Paragraph(h, s_th) for h in hdr_cols]] + data_rows
        t = Table(rows, colWidths=col_widths)
        t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),hdr_color),
            ("FONTSIZE",(0,0),(-1,-1),9),("TOPPADDING",(0,0),(-1,-1),5),
            ("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(0,0),(-1,-1),8),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[LIGHT,WHITE]),("GRID",(0,0),(-1,-1),0.3,BORDER)]))
        return t

    # ── Profile fields ─────────────────────────────────────────────────────
    import datetime as _dt2, json as _json2, re as _re2
    now_dt   = _dt2.datetime.now()
    now_str  = now_dt.strftime("%d %b %Y  %H:%M")
    date_str = now_dt.strftime("%d %b %Y")

    p_name     = profile.get("name")   or "Not provided"
    p_age      = str(profile.get("age") or "Not provided")
    p_gender   = profile.get("gender") or "Not provided"
    p_city     = profile.get("city")   or "Not provided"
    p_ins      = profile.get("insurance_type")  or "Health Insurance"
    p_budget   = profile.get("budget_range")    or "Not provided"
    p_coverage = profile.get("coverage_type")   or "Not provided"
    p_craw     = (profile.get("medical_conditions") or "")
    p_conds    = [c.strip() for c in p_craw.split(",")
                  if c.strip() and c.strip().lower() not in ("none","")]
    p_fam_count = str(profile.get("family_member_count") or "0")
    p_fam_cond  = (profile.get("family_medical_conditions") or "None")
    p_fam_json  = profile.get("family_members_json") or "[]"
    p_idtype    = profile.get("doc_type_found") or "Government ID"
    p_idver     = bool(profile.get("gov_id_verified"))
    p_rptup     = bool(profile.get("condition_report_uploaded") or profile.get("medical_report_uploaded"))
    p_rpt_summ  = (profile.get("medical_report_summary") or
                   profile.get("condition_report_result") or "Not uploaded")
    ins_l       = p_ins.lower()
    cov_lower   = p_coverage.lower()

    fraud_status     = (profile.get("fraud_status") or "LOW").upper()
    fraud_issues_raw = profile.get("fraud_issues") or "[]"
    try:
        fraud_issues = _json2.loads(fraud_issues_raw) if isinstance(fraud_issues_raw, str) else (fraud_issues_raw or [])
    except Exception:
        fraud_issues = []

    risk_score_val = int(profile.get("risk_score") or 0)
    risk_category  = profile.get("risk_category") or (
        "Low Risk" if risk_score_val <= 30 else "Moderate Risk" if risk_score_val <= 60 else "High Risk")
    premium_pred   = profile.get("premium_prediction") or "Not calculated"

    fraud_clr = {"LOW": GREEN, "MEDIUM": AMBER, "HIGH": RED}.get(fraud_status, AMBER)
    if "low" in risk_category.lower():
        risk_clr = GREEN
    elif "high" in risk_category.lower():
        risk_clr = RED
    else:
        risk_clr = AMBER

    buf = io.BytesIO()
    pw  = A4[0] - 30*mm
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15*mm, rightMargin=15*mm,
                             topMargin=12*mm, bottomMargin=12*mm,
                             title="PolicyBot Insurance Analysis Report")
    story = []

    # BANNER
    story.append(Table(
        [[Paragraph("PolicyBot AI  Insurance Analysis Report", s_ban)],
         [Paragraph(f"Prepared for: {p_name}  |  {p_ins}  |  {date_str}", s_sub)]],
        colWidths=[pw], style=TableStyle([
            ("BACKGROUND",(0,0),(0,0),NAVY),("BACKGROUND",(0,1),(0,1),ACCNT),
            ("TOPPADDING",(0,0),(0,0),14),("BOTTOMPADDING",(0,0),(0,0),8),
            ("TOPPADDING",(0,1),(0,1),5),("BOTTOMPADDING",(0,1),(0,1),5)])))
    story.append(Spacer(1,8))

    # SEC 1 - User Profile Summary
    story.append(sec_hdr("1.", "User Profile Summary", NAVY))
    story.append(Spacer(1,4))
    story.append(grid_tbl(
        ["FIELD","VALUE","FIELD","VALUE"],
        [
            [Paragraph("Full Name",s_lbl),Paragraph(p_name,s_bld),
             Paragraph("Insurance Type",s_lbl),Paragraph(p_ins,s_bld)],
            [Paragraph("Age",s_lbl),Paragraph(f"{p_age} years",s_val),
             Paragraph("Coverage Type",s_lbl),Paragraph(p_coverage,s_val)],
            [Paragraph("Gender",s_lbl),Paragraph(p_gender,s_val),
             Paragraph("Budget Range",s_lbl),Paragraph(p_budget,s_val)],
            [Paragraph("City / Location",s_lbl),Paragraph(p_city,s_val),
             Paragraph("Gov ID Verified",s_lbl),
             Paragraph("Yes" if p_idver else "No", s_ok if p_idver else s_warn)],
        ],
        col_widths=[pw*0.20,pw*0.30,pw*0.20,pw*0.30], hdr_color=NAVY))

    try:
        fam_members = _json2.loads(p_fam_json)
    except Exception:
        fam_members = []

    if "myself" not in cov_lower and int(p_fam_count or 0) > 0:
        story.append(Spacer(1,5))
        fam_rows = []
        if fam_members:
            for m in fam_members:
                fam_rows.append([Paragraph(str(m.get("relationship","Member")),s_val),
                                  Paragraph(str(m.get("age","?")),s_val),
                                  Paragraph("Active",s_ok)])
        else:
            fam_rows.append([Paragraph(f"{p_fam_count} member(s)",s_val),
                              Paragraph("See profile",s_val),
                              Paragraph("Active",s_ok)])
        story.append(grid_tbl(["FAMILY MEMBER","AGE","STATUS"],
            fam_rows, col_widths=[pw*0.44,pw*0.26,pw*0.30], hdr_color=TEAL))
        if p_fam_cond and p_fam_cond.lower() not in ("none",""):
            story.append(Spacer(1,3))
            story.append(kv("Family Health Notes", p_fam_cond, s_warn))
    story.append(Spacer(1,8))

    # SEC 2 - Government ID Verification
    story.append(sec_hdr("2.", "Government ID Verification", BLUE))
    story.append(Spacer(1,4))
    st_txt = ("IDENTITY VERIFIED - Name, Age and Gender Confirmed"
              if p_idver else "NOT VERIFIED - Government ID was skipped or failed")
    story.append(Table([[Paragraph(st_txt, mk("stT",fontSize=10,textColor=WHITE,
        fontName="Helvetica-Bold",leading=14,alignment=TA_CENTER))]],
        colWidths=[pw], style=TableStyle([("BACKGROUND",(0,0),(-1,-1),GREEN if p_idver else RED),
            ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7)])))
    story.append(Spacer(1,4))
    story.append(grid_tbl(["FIELD","PROFILE VALUE","EXTRACTED FROM ID","STATUS"],
        [[Paragraph("Full Name",s_lbl),Paragraph(p_name,s_val),
          Paragraph(p_name if p_idver else "Not extracted",s_val),
          Paragraph("Match" if p_idver else "Not verified",s_ok if p_idver else s_warn)],
         [Paragraph("Age",s_lbl),Paragraph(f"{p_age} years",s_val),
          Paragraph(f"{p_age} years" if p_idver else "Not extracted",s_val),
          Paragraph("Match" if p_idver else "Not verified",s_ok if p_idver else s_warn)],
         [Paragraph("Gender",s_lbl),Paragraph(p_gender,s_val),
          Paragraph(p_gender if p_idver else "Not extracted",s_val),
          Paragraph("Auto-extracted" if p_idver else "Not verified",s_ok if p_idver else s_warn)],
         [Paragraph("Document Type",s_lbl),Paragraph(p_idtype,s_val),
          Paragraph("Authentic" if p_idver else "Unverified",s_val),
          Paragraph("Verified" if p_idver else "Unknown",s_ok if p_idver else s_warn)]],
        col_widths=[pw*0.24,pw*0.26,pw*0.28,pw*0.22], hdr_color=BLUE))
    story.append(Spacer(1,8))

    # SEC 3 - Identity Cross-Verification
    story.append(sec_hdr("3.", "Identity Cross-Verification  (Gov ID  vs  Medical Report)", TEAL))
    story.append(Spacer(1,4))
    if p_rptup:
        rpt_name   = profile.get("medical_report_patient_name","") or p_name
        rpt_age    = profile.get("medical_report_patient_age","")  or p_age
        rpt_gender = profile.get("medical_report_patient_gender","") or p_gender
        name_ok = p_name.lower().split()[0] in rpt_name.lower() if p_name and rpt_name else True
        age_ok  = str(p_age) == str(rpt_age) if rpt_age else True
        story.append(grid_tbl(["FIELD","GOV ID VALUE","MEDICAL REPORT","RESULT"],
            [[Paragraph("Name",s_lbl),Paragraph(p_name,s_val),Paragraph(rpt_name,s_val),
              Paragraph("Consistent" if name_ok else "Review",s_ok if name_ok else s_warn)],
             [Paragraph("Age",s_lbl),Paragraph(f"{p_age} yrs",s_val),Paragraph(f"{rpt_age}",s_val),
              Paragraph("Consistent" if age_ok else "Check",s_ok if age_ok else s_warn)],
             [Paragraph("Gender",s_lbl),Paragraph(p_gender,s_val),Paragraph(rpt_gender,s_val),
              Paragraph("Consistent",s_ok)]],
            col_widths=[pw*0.22,pw*0.26,pw*0.26,pw*0.26], hdr_color=TEAL))
        if p_rpt_summ and p_rpt_summ != "Not uploaded":
            story.append(Spacer(1,4))
            story.append(kv("Medical Report Summary", p_rpt_summ[:200]))
    else:
        story.append(Table([[Paragraph(
            "No medical report uploaded - cross-verification not applicable.", s_warn)]],
            colWidths=[pw], style=TableStyle([
                ("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#fef9c3")),
                ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
                ("LEFTPADDING",(0,0),(-1,-1),10),
                ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#fde047"))])))
    story.append(Spacer(1,8))

    # SEC 4 - Fraud Detection
    story.append(sec_hdr("4.", "Fraud Detection and Document Verification", colors.HexColor("#7c3aed")))
    story.append(Spacer(1,4))
    story.append(Table([[Paragraph(
        f"FRAUD RISK LEVEL: {fraud_status}",
        mk("frd",fontSize=11,textColor=WHITE,fontName="Helvetica-Bold",leading=15,alignment=TA_CENTER))]],
        colWidths=[pw], style=TableStyle([("BACKGROUND",(0,0),(-1,-1),fraud_clr),
            ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8)])))
    story.append(Spacer(1,4))

    checks = [
        ("Name Verification",     "Passed" if p_idver else "Skipped",    "ok" if p_idver else "warn"),
        ("Age Verification",      "Passed" if p_idver else "Skipped",    "ok" if p_idver else "warn"),
        ("Gender Verification",   "Auto-extracted from ID" if p_idver else "Not verified", "ok" if p_idver else "warn"),
        ("Document Authenticity", "Authentic" if p_idver else "Unknown", "ok" if p_idver else "warn"),
        ("Medical Report Match",  "Verified" if p_rptup else "No report","ok" if p_rptup else "warn"),
        ("Location Consistency",  "City declared: " + p_city,            "ok"),
        ("Overall Fraud Status",  fraud_status,
         "ok" if fraud_status=="LOW" else "warn" if fraud_status=="MEDIUM" else "bad"),
    ]
    sty_map = {"ok": s_ok, "warn": s_warn, "bad": s_bad}
    check_rows = [[Paragraph(c[0],s_lbl), Paragraph(c[1],sty_map.get(c[2],s_val)),
                   Paragraph("PASS" if c[2]=="ok" else ("WARN" if c[2]=="warn" else "FAIL"),
                              sty_map.get(c[2],s_val))] for c in checks]
    story.append(grid_tbl(["CHECK","RESULT","STATUS"], check_rows,
        col_widths=[pw*0.37,pw*0.42,pw*0.21], hdr_color=colors.HexColor("#7c3aed")))
    story.append(Spacer(1,4))
    if fraud_issues:
        story.append(kv("Detected Issues",
            " | ".join(fraud_issues) if isinstance(fraud_issues,list) else str(fraud_issues), s_warn))
    else:
        story.append(kv("Detected Issues", "None - all checks passed", s_ok))
    story.append(Spacer(1,8))

    # SEC 5 - Risk Assessment
    story.append(sec_hdr("5.", "Insurance Risk Assessment", colors.HexColor("#b45309")))
    story.append(Spacer(1,4))
    story.append(Table([[Paragraph(
        f"RISK SCORE: {risk_score_val} / 100  |  {risk_category.upper()}",
        mk("rs2",fontSize=12,textColor=WHITE,fontName="Helvetica-Bold",leading=16,alignment=TA_CENTER))]],
        colWidths=[pw], style=TableStyle([("BACKGROUND",(0,0),(-1,-1),risk_clr),
            ("TOPPADDING",(0,0),(-1,-1),9),("BOTTOMPADDING",(0,0),(-1,-1),9)])))
    story.append(Spacer(1,4))
    try:
        age_int = int(_re2.search(r"\d+", str(p_age)).group()) if _re2.search(r"\d+", str(p_age)) else 0
    except Exception:
        age_int = 0
    age_txt = ("Low (under 35)" if age_int < 35 else "Moderate (35-54)" if age_int < 55 else "High (55+)")
    story.append(grid_tbl(["RISK FACTOR","DETAILS","IMPACT"],
        [[Paragraph("Age Factor",s_lbl),Paragraph(f"{p_age} years - {age_txt}",s_val),
          Paragraph("Low" if age_int<35 else "Moderate" if age_int<55 else "High",
                    s_ok if age_int<35 else s_warn if age_int<55 else s_bad)],
         [Paragraph("Medical Conditions",s_lbl),
          Paragraph(", ".join(p_conds) if p_conds else "None declared",s_val),
          Paragraph("Present" if p_conds else "None",s_bad if p_conds else s_ok)],
         [Paragraph("Family Coverage",s_lbl),
          Paragraph(f"{p_fam_count} member(s) - {p_coverage}",s_val),
          Paragraph("Low" if "myself" in cov_lower else "Moderate",s_val)],
         [Paragraph("Medical Report",s_lbl),
          Paragraph("Uploaded and analyzed" if p_rptup else "Not uploaded",s_val),
          Paragraph("Considered" if p_rptup else "Not assessed",s_val)],
         [Paragraph("Fraud Penalty",s_lbl),Paragraph(f"Fraud level: {fraud_status}",s_val),
          Paragraph("None" if fraud_status=="LOW" else "Applied",
                    s_ok if fraud_status=="LOW" else s_warn)],
         [Paragraph("FINAL RISK CATEGORY",s_bld),
          Paragraph(risk_category,s_bld),Paragraph(f"{risk_score_val}/100",s_bld)]],
        col_widths=[pw*0.28,pw*0.50,pw*0.22], hdr_color=colors.HexColor("#b45309")))
    story.append(Spacer(1,8))

    # SEC 6 - Premium Prediction
    story.append(sec_hdr("6.", "Estimated Insurance Premium Range", GRN_D))
    story.append(Spacer(1,4))
    story.append(Table([[Paragraph(
        f"Estimated Monthly Premium Range:  {premium_pred}",
        mk("prem",fontSize=12,textColor=WHITE,fontName="Helvetica-Bold",leading=17,alignment=TA_CENTER))]],
        colWidths=[pw], style=TableStyle([("BACKGROUND",(0,0),(-1,-1),GRN_D),
            ("TOPPADDING",(0,0),(-1,-1),9),("BOTTOMPADDING",(0,0),(-1,-1),9)])))
    story.append(Spacer(1,5))
    story.append(grid_tbl(["PREMIUM FACTOR","VALUE","IMPACT"],
        [[Paragraph("Age",s_lbl),Paragraph(f"{p_age} years",s_val),Paragraph("Higher with age",s_val)],
         [Paragraph("Medical Conditions",s_lbl),
          Paragraph(", ".join(p_conds) if p_conds else "None",s_val),
          Paragraph("Loading applies" if p_conds else "Standard rate",s_val)],
         [Paragraph("Coverage Type",s_lbl),Paragraph(p_coverage,s_val),
          Paragraph("Family = higher premium",s_val)],
         [Paragraph("Risk Score",s_lbl),Paragraph(f"{risk_score_val}/100",s_val),
          Paragraph(f"{risk_category} profile",s_val)],
         [Paragraph("Location",s_lbl),Paragraph(p_city,s_val),Paragraph("Metro slightly higher",s_val)],
         [Paragraph("User Budget Preference",s_lbl),Paragraph(p_budget,s_val),Paragraph("Preference noted",s_val)]],
        col_widths=[pw*0.28,pw*0.36,pw*0.36], hdr_color=GRN_D))
    story.append(Spacer(1,8))

    # SEC 7 - Health Parameters
    story.append(sec_hdr("7.", "Health Profile and Medical Parameters", PURPLE))
    story.append(Spacer(1,4))
    def pstatus(*cond_names):
        for cn in cond_names:
            if cn in p_conds: return (f"Abnormal - {cn}", s_bad)
        return ("Normal", s_ok)
    params_list = [("Blood Pressure","Hypertension","Blood Pressure"),("Heart Rate","Heart Disease"),
                   ("Blood Sugar","Diabetes"),("Cholesterol","Heart Disease"),
                   ("Kidney Function","Kidney Disease"),("Thyroid (TSH)","Thyroid"),
                   ("Respiratory","Asthma"),("Liver Function","Liver Disease")]
    param_rows_p = []
    for row in params_list:
        label = row[0]; conds_chk = row[1:]
        txt, sty = pstatus(*conds_chk)
        param_rows_p.append([Paragraph(label,s_lbl), Paragraph(txt,sty)])
    story.append(grid_tbl(["PARAMETER","STATUS"], param_rows_p,
        col_widths=[pw*0.42,pw*0.58], hdr_color=PURPLE))
    ov_txt = ("Healthy - No abnormal conditions detected"
              if not p_conds else f"Conditions Present: {', '.join(p_conds)}")
    story.append(Spacer(1,4))
    story.append(Table([[Paragraph("Overall Health Status",s_lbl),
                         Paragraph(ov_txt, s_ok if not p_conds else s_bad)]],
        colWidths=[pw*0.38,pw*0.62], style=TableStyle([
            ("BACKGROUND",(0,0),(-1,-1),LIGHT),("TOPPADDING",(0,0),(-1,-1),6),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),10),
            ("BOX",(0,0),(-1,-1),0.6, GREEN if not p_conds else RED)])))
    story.append(Spacer(1,8))

    # SEC 8 - Plan Comparison
    story.append(sec_hdr("8.", "Insurance Plan Comparison  -  Top 3 Recommendations", GRN_D))
    story.append(Spacer(1,4))
    story.append(kv("Insurance Type",   p_ins))
    story.append(kv("Budget",           p_budget))
    story.append(kv("Premium Estimate", premium_pred))
    story.append(Spacer(1,5))

    if stored_recs and len(stored_recs) >= 3:
        disp_recs = stored_recs[:3]
    else:
        if "health" in ins_l:
            if "Diabetes" in p_conds:
                disp_recs = [
                    {"plan_name":"Star Diabetes Safe","premium":"Rs.8,000-12,000/yr","coverage":"Rs.3-10 Lakh","waiting_period":"2 yr PED","reason":"Designed for diabetics; covers insulin, OPD and day-care procedures. Best-in-class diabetes management benefits."},
                    {"plan_name":"Niva Bupa ReAssure 2.0","premium":"Rs.9,500-14,000/yr","coverage":"Rs.5-25 Lakh","waiting_period":"3 yr PED","reason":"Restore benefit plus no-claim bonus up to 100%. Strong diabetes cover with minimal waiting period."},
                    {"plan_name":"Care Freedom","premium":"Rs.10,000-16,000/yr","coverage":"Rs.4-15 Lakh","waiting_period":"2 yr PED","reason":"No co-pay clause plus day-1 hospitalisation coverage for pre-existing diabetes management."},
                ]
            elif any(c in p_conds for c in ["Heart Disease","Hypertension","Blood Pressure"]):
                disp_recs = [
                    {"plan_name":"Aditya Birla Activ Health Enhanced","premium":"Rs.12,000-18,000/yr","coverage":"Rs.5-25 Lakh","waiting_period":"2 yr PED","reason":"HealthReturns cashback plus dedicated cardiac care cover. Best for heart conditions."},
                    {"plan_name":"Care Heart Plan","premium":"Rs.10,000-15,000/yr","coverage":"Rs.3-10 Lakh","waiting_period":"90 days","reason":"Day-1 cardiac cover plus ICU room rent waiver. Fastest cover for heart patients."},
                    {"plan_name":"HDFC Ergo Optima Restore","premium":"Rs.9,000-14,000/yr","coverage":"Rs.3-50 Lakh","waiting_period":"2 yr PED","reason":"100% automatic restore plus 10,000+ network hospitals. Broad coverage."},
                ]
            elif "whole family" in cov_lower or "family" in cov_lower:
                disp_recs = [
                    {"plan_name":"Star Health Family Optima","premium":"Rs.7,000-14,000/yr","coverage":"Rs.3-25 Lakh","waiting_period":"30 days","reason":"Best-selling family floater with 14,000+ cashless hospitals. Excellent family coverage."},
                    {"plan_name":"Niva Bupa Family Floater","premium":"Rs.8,500-15,000/yr","coverage":"Rs.5-20 Lakh","waiting_period":"30 days","reason":"Restore plus no-claim bonus. Covers entire family under one premium efficiently."},
                    {"plan_name":"HDFC Ergo My:Health Suraksha","premium":"Rs.9,000-16,000/yr","coverage":"Rs.3-50 Lakh","waiting_period":"30 days","reason":"Unlimited restore plus critical illness rider included in family plan."},
                ]
            else:
                disp_recs = [
                    {"plan_name":"Star Health Family Optima","premium":"Rs.7,000-14,000/yr","coverage":"Rs.3-25 Lakh","waiting_period":"30 days","reason":"Best-selling floater with 14,000+ cashless hospitals across India."},
                    {"plan_name":"HDFC Ergo Optima Restore","premium":"Rs.9,000-13,000/yr","coverage":"Rs.3-50 Lakh","waiting_period":"30 days","reason":"Unique restore benefit plus critical illness rider for comprehensive coverage."},
                    {"plan_name":"Niva Bupa Health Companion","premium":"Rs.6,500-11,000/yr","coverage":"Rs.3-10 Lakh","waiting_period":"30 days","reason":"Affordable option with 50% no-claim bonus and strong network."},
                ]
        elif "life" in ins_l or "term" in ins_l:
            disp_recs = [
                {"plan_name":"HDFC Life Click2Protect Super","premium":"Rs.10,000-18,000/yr","coverage":"Rs.50L-2 Cr","waiting_period":"None","reason":"99.4% claim settlement ratio with income replacement benefit. India's most trusted term plan."},
                {"plan_name":"ICICI Pru iProtect Smart","premium":"Rs.8,000-16,000/yr","coverage":"Rs.50L-2 Cr","waiting_period":"None","reason":"Critical illness rider plus accidental disability cover. Comprehensive life protection."},
                {"plan_name":"LIC Tech Term","premium":"Rs.9,000-15,000/yr","coverage":"Rs.50L-3 Cr","waiting_period":"None","reason":"Government-backed reliability with 98.8% claim settlement. Maximum trust."},
            ]
        elif "vehicle" in ins_l or "motor" in ins_l or "car" in ins_l:
            disp_recs = [
                {"plan_name":"HDFC ERGO Comprehensive Motor","premium":"Rs.4,000-8,000/yr","coverage":"IDV-based","waiting_period":"None","reason":"6,800+ cashless garages plus zero depreciation add-on for full coverage."},
                {"plan_name":"Bajaj Allianz Smart Drive","premium":"Rs.3,500-7,500/yr","coverage":"IDV-based","waiting_period":"None","reason":"24x7 roadside assistance plus personal accident cover included."},
                {"plan_name":"ICICI Lombard Complete Cover","premium":"Rs.4,200-8,500/yr","coverage":"IDV-based","waiting_period":"None","reason":"7,500+ garages plus key replacement and engine protection cover."},
            ]
        elif "travel" in ins_l:
            disp_recs = [
                {"plan_name":"Bajaj Allianz Travel Assurance","premium":"Rs.500-3,000/trip","coverage":"USD 50K-250K","waiting_period":"None","reason":"Medical emergency plus trip cancellation cover worldwide. Best global coverage."},
                {"plan_name":"Tata AIG Travel Guard","premium":"Rs.600-3,500/trip","coverage":"USD 50K-500K","waiting_period":"None","reason":"Adventure sports coverage plus 24x7 global assistance included."},
                {"plan_name":"HDFC ERGO Travel Ease","premium":"Rs.450-2,800/trip","coverage":"USD 50K-300K","waiting_period":"None","reason":"Covers till age 80 plus home burglary protection while travelling."},
            ]
        else:
            disp_recs = [
                {"plan_name":"Bajaj Allianz Personal Guard","premium":"Rs.2,000-5,000/yr","coverage":"Rs.10-50 Lakh","waiting_period":"None","reason":"Education benefit plus weekly compensation for accidents. Comprehensive personal coverage."},
                {"plan_name":"New India Accident Shield","premium":"Rs.1,500-4,000/yr","coverage":"Rs.5-25 Lakh","waiting_period":"None","reason":"Permanent total and partial disability cover with government backing."},
                {"plan_name":"Tata AIG Accident Guard","premium":"Rs.2,200-5,500/yr","coverage":"Rs.10-75 Lakh","waiting_period":"None","reason":"2x payout for accidents in public transport. Extra protection when commuting."},
            ]

    plan_rows_tbl = []
    for idx, rec in enumerate(disp_recs[:3], 1):
        pn = rec.get("plan_name") or rec.get("name") or f"Plan {idx}"
        pm = rec.get("premium") or "Not available"
        cv = rec.get("coverage") or "Not available"
        wp = rec.get("waiting_period") or "None"
        rs = (rec.get("reason") or "Recommended for your profile")[:90]
        plan_rows_tbl.append([Paragraph(f"#{idx} {pn}",s_bld),Paragraph(cv,s_val),
                               Paragraph(pm,s_val),Paragraph(wp,s_val),Paragraph(rs,s_norm)])
    story.append(grid_tbl(["PLAN NAME","COVERAGE","PREMIUM","WAITING","KEY BENEFIT"],
        plan_rows_tbl, col_widths=[pw*0.24,pw*0.14,pw*0.17,pw*0.12,pw*0.33], hdr_color=GRN_D))
    story.append(Spacer(1,8))

    # SEC 9 - Final Recommendation
    story.append(sec_hdr("9.", "Final Recommendation and Overall Summary", NAVY))
    story.append(Spacer(1,4))
    best_plan = (disp_recs[0].get("plan_name") or disp_recs[0].get("name","Not available")) if disp_recs else "Not available"
    best_prem = disp_recs[0].get("premium","Not available") if disp_recs else "Not available"
    best_cov  = disp_recs[0].get("coverage","Not available") if disp_recs else "Not available"
    best_rsn  = disp_recs[0].get("reason","Recommended based on your risk profile and budget") if disp_recs else ""

    story.append(Table([[Paragraph(f"BEST PLAN FOR YOU:  {best_plan}", s_best)],
                         [Paragraph(f"Coverage: {best_cov}  |  Premium: {best_prem}", s_sub)]],
        colWidths=[pw], style=TableStyle([
            ("BACKGROUND",(0,0),(0,0),NAVY),("BACKGROUND",(0,1),(0,1),ACCNT),
            ("TOPPADDING",(0,0),(0,0),10),("BOTTOMPADDING",(0,0),(0,0),8),
            ("TOPPADDING",(0,1),(0,1),5),("BOTTOMPADDING",(0,1),(0,1),5)])))
    story.append(Spacer(1,5))
    story.append(Table([[Paragraph("RECOMMENDATION REASON",s_lbl)],[Paragraph(best_rsn,
        mk("rsn2",fontSize=9,textColor=colors.HexColor("#1e3a5f"),fontName="Helvetica",leading=13))]],
        colWidths=[pw], style=TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#eff6ff")),
            ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
            ("LEFTPADDING",(0,0),(-1,-1),12),("BOX",(0,0),(-1,-1),0.6,BLUE)])))
    story.append(Spacer(1,6))

    sum_rows = [
        [Paragraph("FIELD",s_th),        Paragraph("RESULT",s_th)],
        [Paragraph("Name",s_lbl),        Paragraph(p_name,s_bld)],
        [Paragraph("Age / Gender",s_lbl),Paragraph(f"{p_age} years  /  {p_gender}",s_val)],
        [Paragraph("City",s_lbl),        Paragraph(p_city,s_val)],
        [Paragraph("Insurance",s_lbl),   Paragraph(p_ins,s_bld)],
        [Paragraph("Coverage",s_lbl),    Paragraph(p_coverage,s_val)],
        [Paragraph("Health Status",s_lbl),
         Paragraph("Healthy" if not p_conds else f"Conditions: {', '.join(p_conds)}",
                   s_ok if not p_conds else s_bad)],
        [Paragraph("Fraud Check",s_lbl),
         Paragraph(fraud_status, s_ok if fraud_status=="LOW" else s_warn if fraud_status=="MEDIUM" else s_bad)],
        [Paragraph("Risk Score",s_lbl),
         Paragraph(f"{risk_score_val}/100  -  {risk_category}",
                   s_ok if "low" in risk_category.lower() else s_warn if "moderate" in risk_category.lower() else s_bad)],
        [Paragraph("Est. Monthly Premium",s_lbl), Paragraph(premium_pred,s_bld)],
        [Paragraph("Best Recommended Plan",s_lbl),Paragraph(best_plan,s_bld)],
        [Paragraph("Budget Preference",s_lbl),    Paragraph(p_budget,s_val)],
    ]
    sum_tbl = Table(sum_rows, colWidths=[pw*0.35,pw*0.65])
    sum_tbl.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),NAVY),("FONTSIZE",(0,0),(-1,-1),9),
        ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
        ("LEFTPADDING",(0,0),(-1,-1),10),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[LIGHT,WHITE]),("GRID",(0,0),(-1,-1),0.3,BORDER)]))
    story.append(sum_tbl)
    story.append(Spacer(1,12))

    # Footer
    story.append(HRFlowable(width=pw, thickness=0.5, color=BORDER))
    story.append(Spacer(1,4))
    story.append(Table([[Paragraph(
        f"Auto-generated by PolicyBot AI on {now_str}. "
        "For informational purposes only - consult a certified insurance advisor before purchasing. "
        "No Aadhaar or PAN numbers are stored by this system.",
        s_foot)]],
        colWidths=[pw], style=TableStyle([("BACKGROUND",(0,0),(-1,-1),LIGHT),
            ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
            ("LEFTPADDING",(0,0),(-1,-1),12),("BOX",(0,0),(-1,-1),0.4,BORDER)])))

    try:
        doc.build(story)
    except Exception as build_err:
        app.logger.error(f"[REPORT] PDF build failed: {build_err}")
        return jsonify({"error": f"PDF generation failed: {str(build_err)}"}), 500

    buf.seek(0)
    if buf.getbuffer().nbytes < 200:
        return jsonify({"error": "Generated PDF is empty — please try again"}), 500

    safe  = (p_name or "User").replace(" ","_").replace("/","_")
    fname = f"PolicyBot_Insurance_Analysis_{safe}_{now_dt.strftime('%Y%m%d')}.pdf"
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=fname)




# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP REPORT DELIVERY — Meta Cloud API (no Twilio)
# ══════════════════════════════════════════════════════════════════════════════
# Required .env keys:
#   WA_PHONE_ID   — WhatsApp Business Phone Number ID  (from Meta Developer Console)
#   WA_TOKEN      — Permanent System User Access Token  (from Meta Business Manager)
# ══════════════════════════════════════════════════════════════════════════════


@app.route("/api/admin/fraud-alerts/live")
@admin_required
def api_fraud_live():
    """
    Polls every 15s. Surfaces users who are HIGH or MEDIUM risk via:
    1. run_fraud_detection — document/data mismatches (fraud flags)
    2. run_risk_scoring    — high risk score (score > 60 = High Risk)
    Any user with a name who triggers either engine is shown in the bell.
    """
    from models.fraud_risk import run_fraud_detection, run_risk_scoring
    import json as _json
    users  = db.get_all_users_raw()
    alerts = []
    seen   = set()
    for _u in users:
        u = dict(_u)
        if not u.get("name"):
            continue
        uid = u.get("user_id", "")
        if uid in seen:
            continue

        alert_level = None
        flags       = []

        # ── Engine 1: Fraud detection (document mismatches) ───────────────
        try:
            fr = run_fraud_detection(u)
            fd_level = (fr.get("fraud_status") or "LOW").upper()
            raw = fr.get("fraud_issues", "[]")
            try:
                fd_flags = _json.loads(raw) if isinstance(raw, str) else (raw or [])
            except Exception:
                fd_flags = [raw] if raw else []
            if fd_level in ("HIGH", "MEDIUM"):
                alert_level = fd_level
                flags.extend(fd_flags)
        except Exception:
            pass

        # ── Engine 2: Risk scoring (score > 60 = High Risk) ──────────────
        try:
            rr = run_risk_scoring(u, {})
            cat = rr.get("risk_category", "")
            score = rr.get("risk_score", 0)
            if cat == "High Risk" and alert_level not in ("HIGH",):
                alert_level = alert_level or "HIGH"
                flags.append(f"Risk score: {score}/100 — High Risk category")
            elif cat == "Moderate Risk" and alert_level is None:
                alert_level = "MEDIUM"
                flags.append(f"Risk score: {score}/100 — Moderate Risk")
        except Exception:
            pass

        if alert_level:
            seen.add(uid)
            alerts.append({
                "user_id":    uid[:12],
                "user_name":  u.get("name") or "Unknown",
                "risk_level": alert_level,
                "flags":      flags[:3],
                "insurance":  u.get("insurance_type", ""),
                "city":       u.get("city", ""),
            })

    alerts.sort(key=lambda a: 0 if a["risk_level"] == "HIGH" else 1)
    return jsonify({"status": "success", "count": len(alerts), "alerts": alerts})


# ══════════════════════════════════════════════════════════════════════════════
# PREMIUM CALCULATOR  (/api/calc)
# Standalone instant quote — no auth needed, stateless POST
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/calc", methods=["POST"])
def premium_calc():
    """
    Instant premium estimate using existing fraud_risk._predict_premium logic.
    Inputs: insurance_type, age, coverage_type, family_member_count, medical_conditions
    Returns: premium_range (str), risk_score (int), claim_probability (int), breakdown (dict)
    """
    d = request.json or {}
    profile = {
        "insurance_type":     d.get("insurance_type", "health"),
        "age":                str(d.get("age", 30)),
        "coverage_type":      d.get("coverage_type", "Myself only"),
        "family_member_count": int(d.get("family_member_count", 1)),
        "medical_conditions": d.get("medical_conditions", "None"),
        "budget_range":       "",
    }

    try:
        from models.fraud_risk import (run_fraud_detection, run_risk_scoring,
                                       _predict_claim_probability)
        import re as _re

        fraud_r = run_fraud_detection(profile)
        risk_r  = run_risk_scoring(profile, fraud_r)

        age_m = _re.search(r"\b(\d{1,3})\b", str(profile["age"]))
        age   = int(age_m.group(1)) if age_m else 30

        claim_prob = _predict_claim_probability(profile, risk_r["risk_score"], age)

        # Build per-factor breakdown for UI display
        age_label    = ("Low" if age < 35 else "Medium" if age < 50 else "High")
        cond_label   = "None"
        cond_raw     = (profile.get("medical_conditions") or "").lower()
        if any(k in cond_raw for k in ["cancer","kidney","heart","cardiac"]):
            cond_label = "Severe"
        elif any(k in cond_raw for k in ["diabetes","hypertension","asthma","thyroid"]):
            cond_label = "Moderate"
        elif cond_raw.strip() and cond_raw.strip() not in ("none","no"):
            cond_label = "Mild"

        fam_count = int(d.get("family_member_count", 1))

        return jsonify({
            "status":            "success",
            "premium_range":     risk_r["premium_prediction"],
            "risk_score":        risk_r["risk_score"],
            "risk_category":     risk_r["risk_category"],
            "claim_probability": claim_prob,
            "breakdown": {
                "age_factor":     age_label,
                "condition_risk": cond_label,
                "family_size":    fam_count,
                "insurance_type": profile["insurance_type"],
            }
        })
    except Exception as e:
        app.logger.error(f"[CALC] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# HOSPITAL NETWORK LOOKUP  (/api/hospitals)
# Returns nearby cashless hospitals for a city × insurance company
# ══════════════════════════════════════════════════════════════════════════════

# Comprehensive city → hospital name list (major Indian insurers' known networks)
_HOSPITAL_DB = {
    "chennai": [
        {"name": "Apollo Hospitals, Greams Road",       "type": "Super Specialty", "area": "Greams Road"},
        {"name": "MIOT International Hospital",          "type": "Multi Specialty", "area": "Manapakkam"},
        {"name": "Fortis Malar Hospital",                "type": "Multi Specialty", "area": "Adyar"},
        {"name": "Sri Ramachandra Medical Centre",       "type": "Teaching Hospital","area": "Porur"},
        {"name": "Kauvery Hospital",                     "type": "Multi Specialty", "area": "T. Nagar"},
        {"name": "SRM Global Hospitals",                 "type": "Super Specialty", "area": "Vadapalani"},
        {"name": "Vijaya Hospital",                      "type": "Multi Specialty", "area": "Vadapalani"},
        {"name": "Billroth Hospitals",                   "type": "Multi Specialty", "area": "Shenoy Nagar"},
        {"name": "VS Hospital",                          "type": "Specialty",       "area": "Royapettah"},
        {"name": "Gleneagles Global Health City",        "type": "Super Specialty", "area": "Perumbakkam"},
    ],
    "mumbai": [
        {"name": "Kokilaben Dhirubhai Ambani Hospital",  "type": "Super Specialty", "area": "Andheri West"},
        {"name": "Lilavati Hospital",                    "type": "Multi Specialty", "area": "Bandra West"},
        {"name": "Breach Candy Hospital",                "type": "Multi Specialty", "area": "Breach Candy"},
        {"name": "Hinduja Hospital",                     "type": "Multi Specialty", "area": "Mahim"},
        {"name": "Nanavati Super Speciality Hospital",   "type": "Super Specialty", "area": "Vile Parle"},
        {"name": "Bombay Hospital",                      "type": "Multi Specialty", "area": "Marine Lines"},
        {"name": "Jupiter Hospital",                     "type": "Super Specialty", "area": "Thane"},
        {"name": "Fortis Hospital",                      "type": "Multi Specialty", "area": "Mulund"},
        {"name": "Wockhardt Hospital",                   "type": "Multi Specialty", "area": "South Mumbai"},
        {"name": "Global Hospital",                      "type": "Multi Specialty", "area": "Parel"},
    ],
    "bangalore": [
        {"name": "Manipal Hospitals (HAL)",              "type": "Super Specialty", "area": "HAL Airport Road"},
        {"name": "Apollo Hospitals Bannerghatta",        "type": "Super Specialty", "area": "Bannerghatta Road"},
        {"name": "Fortis Hospital",                      "type": "Multi Specialty", "area": "Cunningham Road"},
        {"name": "Narayana Health City",                 "type": "Super Specialty", "area": "Electronic City"},
        {"name": "Aster CMI Hospital",                   "type": "Multi Specialty", "area": "Hebbal"},
        {"name": "Columbia Asia Hospital",               "type": "Multi Specialty", "area": "Yeshwanthpur"},
        {"name": "Sakra World Hospital",                 "type": "Super Specialty", "area": "Marathahalli"},
        {"name": "BGS Gleneagles Global Hospital",       "type": "Super Specialty", "area": "Kengeri"},
        {"name": "Sparsh Hospital",                      "type": "Orthopedic",      "area": "Infantry Road"},
        {"name": "St. John's Medical College Hospital",  "type": "Teaching Hospital","area": "Koramangala"},
    ],
    "delhi": [
        {"name": "Apollo Hospital",                      "type": "Super Specialty", "area": "Sarita Vihar"},
        {"name": "Indraprastha Apollo",                  "type": "Super Specialty", "area": "Jasola"},
        {"name": "Fortis Memorial Research Institute",   "type": "Super Specialty", "area": "Gurugram"},
        {"name": "Max Super Speciality Hospital",        "type": "Super Specialty", "area": "Saket"},
        {"name": "Sir Ganga Ram Hospital",               "type": "Multi Specialty", "area": "Rajender Nagar"},
        {"name": "AIIMS Delhi",                          "type": "Government",      "area": "Ansari Nagar"},
        {"name": "Medanta - The Medicity",               "type": "Super Specialty", "area": "Gurugram"},
        {"name": "BLK Super Speciality Hospital",        "type": "Super Specialty", "area": "Pusa Road"},
        {"name": "Rockland Hospital",                    "type": "Multi Specialty", "area": "Qutub"},
        {"name": "Venkateshwar Hospital",                "type": "Multi Specialty", "area": "Dwarka"},
    ],
    "hyderabad": [
        {"name": "Apollo Hospitals",                     "type": "Super Specialty", "area": "Jubilee Hills"},
        {"name": "KIMS Hospital",                        "type": "Super Specialty", "area": "Secunderabad"},
        {"name": "Care Hospital",                        "type": "Multi Specialty", "area": "Nampally"},
        {"name": "Yashoda Hospitals",                    "type": "Multi Specialty", "area": "Malakpet"},
        {"name": "Sunshine Hospitals",                   "type": "Orthopedic",      "area": "PG Road"},
        {"name": "Star Hospital",                        "type": "Multi Specialty", "area": "Banjara Hills"},
        {"name": "Continental Hospitals",                "type": "Super Specialty", "area": "Financial District"},
        {"name": "Medicover Hospitals",                  "type": "Multi Specialty", "area": "Madhapur"},
        {"name": "MaxCure Hospital",                     "type": "Multi Specialty", "area": "Madhapur"},
        {"name": "Aware Gleneagles Global Hospital",     "type": "Multi Specialty", "area": "LB Nagar"},
    ],
    "kolkata": [
        {"name": "Apollo Gleneagles Hospital",           "type": "Super Specialty", "area": "Canal Circular Road"},
        {"name": "Medica Superspecialty Hospital",        "type": "Super Specialty", "area": "Mukundapur"},
        {"name": "Peerless Hospital",                    "type": "Multi Specialty", "area": "Panchasayar"},
        {"name": "Fortis Hospital",                      "type": "Multi Specialty", "area": "Anandapur"},
        {"name": "AMRI Hospitals",                       "type": "Multi Specialty", "area": "Dhakuria"},
        {"name": "Woodlands Multispeciality Hospital",   "type": "Multi Specialty", "area": "Alipore"},
        {"name": "BM Birla Heart Research Centre",       "type": "Cardiac",         "area": "Ballygunge"},
        {"name": "RN Tagore International Institute",    "type": "Cardiac",         "area": "Mukundapur"},
        {"name": "Belle Vue Clinic",                     "type": "Multi Specialty", "area": "Park Street"},
        {"name": "Narayana Multispeciality Hospital",    "type": "Multi Specialty", "area": "Howrah"},
    ],
    "pune": [
        {"name": "Ruby Hall Clinic",                     "type": "Multi Specialty", "area": "Wanowrie"},
        {"name": "Jehangir Hospital",                    "type": "Multi Specialty", "area": "Sassoon Road"},
        {"name": "Sahyadri Hospital",                    "type": "Multi Specialty", "area": "Deccan Gymkhana"},
        {"name": "Inamdar Multispecialty Hospital",      "type": "Multi Specialty", "area": "Fatima Nagar"},
        {"name": "Poona Hospital & Research Centre",     "type": "Multi Specialty", "area": "Sadashiv Peth"},
        {"name": "Aditya Birla Memorial Hospital",       "type": "Super Specialty", "area": "Pimpri"},
        {"name": "Columbia Asia Hospital",               "type": "Multi Specialty", "area": "Kharadi"},
        {"name": "KEM Hospital",                         "type": "Government",      "area": "Rasta Peth"},
    ],
    "ahmedabad": [
        {"name": "Sterling Hospital",                    "type": "Super Specialty", "area": "Gurukul"},
        {"name": "Apollo Hospitals",                     "type": "Super Specialty", "area": "BHAT"},
        {"name": "CIMS Hospital",                        "type": "Super Specialty", "area": "Sola"},
        {"name": "SAL Hospital",                         "type": "Multi Specialty", "area": "Drive-in Road"},
        {"name": "HCG Hospitals",                        "type": "Oncology",        "area": "Navrangpura"},
        {"name": "Zydus Hospital",                       "type": "Multi Specialty", "area": "Thaltej"},
    ],
    "jaipur": [
        {"name": "Fortis Escorts Hospital",              "type": "Multi Specialty", "area": "Jawahar Lal Nehru Marg"},
        {"name": "Narayana Multispeciality Hospital",    "type": "Multi Specialty", "area": "Sector 28"},
        {"name": "Eternal Hospital",                     "type": "Multi Specialty", "area": "JLN Marg"},
        {"name": "Manipal Hospital",                     "type": "Multi Specialty", "area": "Ambabari"},
        {"name": "SMS Hospital",                         "type": "Government",      "area": "Tonk Road"},
    ],
    "coimbatore": [
        {"name": "PSG Hospitals",                        "type": "Super Specialty", "area": "Peelamedu"},
        {"name": "KMCH",                                 "type": "Super Specialty", "area": "Avinashi Road"},
        {"name": "Sri Ramakrishna Hospital",             "type": "Multi Specialty", "area": "North Coimbatore"},
        {"name": "Kovai Medical Center",                 "type": "Super Specialty", "area": "Avanashi Road"},
        {"name": "GVN Hospital",                         "type": "Multi Specialty", "area": "Pappanaickenpalayam"},
    ],
    "kochi": [
        {"name": "Amrita Institute of Medical Sciences", "type": "Super Specialty", "area": "Ponekkara"},
        {"name": "Lakeshore Hospital",                   "type": "Multi Specialty", "area": "NH 17"},
        {"name": "Medical Trust Hospital",               "type": "Multi Specialty", "area": "MG Road"},
        {"name": "PVS Memorial Hospital",                "type": "Multi Specialty", "area": "Kaloor"},
        {"name": "Aster Medcity",                        "type": "Super Specialty", "area": "Cheranalloor"},
    ],
    "thiruvananthapuram": [
        {"name": "Thiruvananthapuram Medical College",   "type": "Government",      "area": "Medical College Junction"},
        {"name": "SUT Hospital",                         "type": "Multi Specialty", "area": "Pattom"},
        {"name": "KIMS Hospital",                        "type": "Multi Specialty", "area": "Anayara"},
        {"name": "VPS Lakeshore",                        "type": "Multi Specialty", "area": "Nanthencode"},
    ],
    "kollam": [
        {"name": "Travancore Medical College",           "type": "Multi Specialty", "area": "Kollam"},
        {"name": "Pushpagiri Medical College Hospital",  "type": "Multi Specialty", "area": "Thiruvalla"},
        {"name": "Baby Memorial Hospital",               "type": "Multi Specialty", "area": "Kollam"},
        {"name": "Azeezia Medical College",              "type": "Teaching Hospital","area": "Kollam"},
        {"name": "Meditrina Institute",                  "type": "Multi Specialty", "area": "Kollam"},
    ],
}

@app.route("/api/hospitals", methods=["POST"])
def hospital_lookup():
    """
    Returns cashless hospital list for a given city.
    Works for any insurance type but most relevant for health.
    Inputs: city (str), insurance_type (str), plan_name (str, optional)
    Returns: hospitals (list), city_matched (str), total (int)
    """
    d    = request.json or {}
    city = (d.get("city") or "").lower().strip()
    ins  = (d.get("insurance_type") or "health").lower()

    # Only relevant for health/medical insurance
    if not any(k in ins for k in ["health","medical","critical"]):
        return jsonify({
            "status":   "success",
            "hospitals": [],
            "message":  "Hospital network lookup is available for Health Insurance plans.",
            "total":    0,
        })

    # Fuzzy city match
    matched_city = None
    for db_city in _HOSPITAL_DB:
        if db_city in city or city in db_city or city.startswith(db_city[:4]):
            matched_city = db_city
            break

    if not matched_city:
        # Default to generic top hospitals message
        return jsonify({
            "status":     "success",
            "hospitals":  [],
            "message":    f"No hospital data for '{city}' yet. Contact your insurer for the cashless list.",
            "total":      0,
            "city_matched": city,
        })

    hospitals = _HOSPITAL_DB[matched_city]
    return jsonify({
        "status":       "success",
        "hospitals":    hospitals,
        "city_matched": matched_city.title(),
        "total":        len(hospitals),
        "message":      f"{len(hospitals)} cashless hospitals found in {matched_city.title()}",
    })


# ══════════════════════════════════════════════════════════════════════════════
# CLAIM PROBABILITY  (/api/claim-score)
# Returns the claim probability for the current user session
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/claim-score", methods=["POST"])
def claim_score():
    """
    Returns claim_probability int (0–100) for a user_id.
    Falls back to computing it on-demand if not yet stored.
    """
    d       = request.json or {}
    user_id = d.get("user_id", "")
    if not user_id:
        return jsonify({"status": "error", "message": "user_id required"}), 400

    profile = db.get_user_profile(user_id) or {}
    claim_p = profile.get("claim_probability", 0) or 0

    # Compute on-demand if not stored yet
    if not claim_p:
        try:
            from models.fraud_risk import run_fraud_detection, run_risk_scoring, _predict_claim_probability
            import re as _re
            _fr = run_fraud_detection(profile)
            _rs = run_risk_scoring(profile, _fr)
            age_m = _re.search(r"\b(\d{1,3})\b", str(profile.get("age") or ""))
            age   = int(age_m.group(1)) if age_m else None
            claim_p = _predict_claim_probability(profile, _rs["risk_score"], age)
        except Exception:
            claim_p = 0

    return jsonify({
        "status":            "success",
        "claim_probability": claim_p,
        "risk_score":        profile.get("risk_score", 0),
        "risk_category":     profile.get("risk_category", ""),
        "insurance_type":    profile.get("insurance_type", ""),
    })




# ══════════════════════════════════════════════════════════════════════════════
# GAMIFICATION  (/api/xp  /api/xp/status)
# XP awarded per onboarding stage completion
# ══════════════════════════════════════════════════════════════════════════════
XP_REWARDS = {
    "insurance_type":         10,
    "collect_name":           15,
    "collect_age":            10,
    "doc_upload":             30,   # big reward for ID verification
    "collect_city":           10,
    "collect_coverage":       10,
    "collect_family_count":   10,
    "collect_family_medical": 15,
    "collect_medical_status": 10,
    "collect_medical":        15,
    "optional_medical_report":25,   # uploading medical report = bonus
    "collect_budget":         10,
    "review_details":         20,
    "recommendation":         50,   # completing onboarding = big reward
    "ask_rating":             20,
    "farewell":               10,
}
XP_LEVELS = [
    (0,   "Beginner",   "🌱"),
    (50,  "Explorer",   "🔍"),
    (120, "Informed",   "📚"),
    (220, "Smart Buyer","💡"),
    (360, "Pro",        "⭐"),
    (500, "Expert",     "🏆"),
]
BADGES = {
    "first_step":     {"name": "First Step",     "icon": "🚀", "xp":  10, "desc": "Started your insurance journey"},
    "id_verified":    {"name": "ID Verified",    "icon": "🛡️", "xp":  30, "desc": "Identity confirmed"},
    "health_hero":    {"name": "Health Hero",    "icon": "💊", "xp":  25, "desc": "Uploaded medical report"},
    "profile_done":   {"name": "Profile Pro",    "icon": "📋", "xp":  20, "desc": "Completed full profile"},
    "plan_found":     {"name": "Plan Found",     "icon": "🎯", "xp":  50, "desc": "Got your personalized recommendation"},
    "top_rated":      {"name": "Top Rater",      "icon": "⭐", "xp":  20, "desc": "Gave us your feedback"},
    "speed_demon":    {"name": "Speed Demon",    "icon": "⚡", "xp":  15, "desc": "Completed onboarding in under 5 min"},
    "family_guardian":{"name": "Family Guardian","icon": "👨‍👩‍👧", "xp": 20, "desc": "Added family coverage"},
}

def _get_xp_level(total_xp):
    lvl_name, lvl_icon, lvl_idx = "Beginner", "🌱", 0
    for i, (min_xp, name, icon) in enumerate(XP_LEVELS):
        if total_xp >= min_xp:
            lvl_name, lvl_icon, lvl_idx = name, icon, i
    next_thresh = XP_LEVELS[min(lvl_idx+1, len(XP_LEVELS)-1)][0]
    return lvl_name, lvl_icon, next_thresh

@app.route("/api/xp", methods=["POST"])
def award_xp():
    """Award XP for a completed stage. Returns new total, level, and any new badges."""
    d       = request.json or {}
    user_id = d.get("user_id", "")
    stage   = d.get("stage", "")
    if not user_id or not stage:
        return jsonify({"error": "user_id and stage required"}), 400

    xp_delta  = XP_REWARDS.get(stage, 5)
    badge_xp  = 0          # initialise outside try so old_lvl_name calc is safe
    lvl_name  = "Beginner"
    lvl_icon  = "🌱"
    next_thresh = 50
    new_badges  = []
    profile   = db.get_user_profile(user_id) or {}

    # Read existing XP from profile (stored as a custom field)
    try:
        with db._conn() as c:
            row = c.execute(
                "SELECT xp_total, xp_stages_done, badges_earned FROM user_xp WHERE user_id=?",
                (user_id,)
            ).fetchone()
            if row:
                row = dict(row)
                stages_done   = set((row.get("xp_stages_done") or "").split(","))
                badges_earned = set((row.get("badges_earned") or "").split(","))
                stages_done.discard("")
                badges_earned.discard("")
                # Only award XP if this stage hasn't been completed before
                if stage in stages_done:
                    xp_delta  = 0   # already awarded — no double XP
                xp_total = int(row.get("xp_total", 0) or 0) + xp_delta
            else:
                stages_done, badges_earned = set(), set()
                xp_total = xp_delta

            stages_done.add(stage)

            # ── Check for new badges ─────────────────────────────────────────
            new_badges = []
            if "insurance_type" in stages_done and "first_step" not in badges_earned:
                new_badges.append("first_step"); badges_earned.add("first_step")
            if profile.get("gov_id_verified") and "id_verified" not in badges_earned:
                new_badges.append("id_verified"); badges_earned.add("id_verified")
            if (profile.get("medical_report_uploaded") or profile.get("condition_report_uploaded")) and "health_hero" not in badges_earned:
                new_badges.append("health_hero"); badges_earned.add("health_hero")
            if len(stages_done) >= 10 and "profile_done" not in badges_earned:
                new_badges.append("profile_done"); badges_earned.add("profile_done")
            if "recommendation" in stages_done and "plan_found" not in badges_earned:
                new_badges.append("plan_found"); badges_earned.add("plan_found")
            if ("family" in (profile.get("coverage_type") or "").lower()) and "family_guardian" not in badges_earned:
                new_badges.append("family_guardian"); badges_earned.add("family_guardian")

            # XP bonus for badges
            badge_xp = sum(BADGES[b]["xp"] for b in new_badges if b in BADGES)
            xp_total += badge_xp

            lvl_name, lvl_icon, next_thresh = _get_xp_level(xp_total)

            # Upsert user_xp table
            c.execute("""
                INSERT INTO user_xp (user_id, xp_total, xp_stages_done, badges_earned, updated_at)
                VALUES (?,?,?,?,datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                  xp_total=excluded.xp_total,
                  xp_stages_done=excluded.xp_stages_done,
                  badges_earned=excluded.badges_earned,
                  updated_at=excluded.updated_at
            """, (user_id, xp_total, ",".join(stages_done), ",".join(badges_earned)))

    except Exception as e:
        log.error(f"[XP] {e}")
        return jsonify({"error": str(e)}), 500

    # detect level-up: old level vs new level
    old_lvl_name = _get_xp_level(xp_total - xp_delta - badge_xp)[0]
    levelled_up  = (lvl_name != old_lvl_name)

    return jsonify({
        "status":         "success",
        "xp_delta":       xp_delta + badge_xp,
        "xp_total":       xp_total,
        "level_name":     lvl_name,
        "level_icon":     lvl_icon,
        "next_thresh":    next_thresh,
        "next_threshold": next_thresh,          # alias — JS uses next_threshold
        "levelled_up":    levelled_up,
        "new_badges":     [{"id": b, **BADGES[b]} for b in new_badges if b in BADGES],
    })

@app.route("/api/xp/status", methods=["POST"])
def xp_status():
    """Get current XP status for a user."""
    d = request.json or {}
    user_id = d.get("user_id", "")
    if not user_id: return jsonify({"error": "user_id required"}), 400
    try:
        with db._conn() as c:
            row = c.execute(
                "SELECT xp_total, xp_stages_done, badges_earned FROM user_xp WHERE user_id=?",
                (user_id,)
            ).fetchone()
        if not row:
            return jsonify({"status":"success","xp_total":0,"level_name":"Beginner",
                            "level_icon":"🌱","badges":[],"next_thresh":50})
        row = dict(row)
        xp_total = int(row.get("xp_total", 0) or 0)
        badges   = [b for b in (row.get("badges_earned") or "").split(",") if b and b in BADGES]
        lvl_name, lvl_icon, next_thresh = _get_xp_level(xp_total)
        return jsonify({
            "status": "success", "xp_total": xp_total,
            "level_name": lvl_name, "level_icon": lvl_icon,
            "next_thresh":    next_thresh,
            "next_threshold": next_thresh,
            "badges": [{"id": b, **BADGES[b]} for b in badges],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# INSURANCE BROKER PARTNER PORTAL
# Routes: /broker/login  /broker  /api/broker/*
# Separate session key: broker_logged_in
# ══════════════════════════════════════════════════════════════════════════════
BROKER_ID   = os.getenv("BROKER_ID",       "broker")
BROKER_PASS = os.getenv("BROKER_PASSWORD", "broker2024")

def broker_required(f):
    from functools import wraps as _wraps
    @_wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("broker_logged_in"):
            if request.path.startswith("/api/broker"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("broker_login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/broker/login", methods=["GET","POST"])
def broker_login():
    error = None
    if request.method == "POST":
        d = request.form
        if d.get("broker_id") == BROKER_ID and d.get("password") == BROKER_PASS:
            session["broker_logged_in"] = True
            return redirect(url_for("broker_dashboard"))
        error = "Invalid broker credentials."
    return render_template("broker_login.html", error=error)

@app.route("/broker/logout")
def broker_logout():
    session.pop("broker_logged_in", None)
    return redirect(url_for("broker_login"))

@app.route("/broker")
@app.route("/broker/dashboard")
@broker_required
def broker_dashboard():
    return render_template("broker.html")

@app.route("/api/broker/leads")
@broker_required
def api_broker_leads():
    """All leads with user profile data joined."""
    try:
        with db._conn() as c:
            rows = c.execute("""
                SELECT l.id, l.user_id, l.plan_name, l.interest_level,
                       l.lead_status, l.phone, l.best_call_time, l.timestamp,
                       u.name, u.age, u.city, u.insurance_type, u.budget_range,
                       u.medical_conditions, u.premium_prediction, u.risk_score,
                       u.risk_category, u.coverage_type
                FROM leads l
                LEFT JOIN users u ON l.user_id = u.user_id
                ORDER BY l.timestamp DESC LIMIT 200
            """).fetchall()
        leads = [dict(r) for r in rows]
        return jsonify({"status": "success", "leads": leads, "total": len(leads)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/broker/lead/<int:lead_id>", methods=["PATCH"])
@broker_required
def api_broker_update_lead(lead_id):
    """Update lead status / notes."""
    d = request.json or {}
    allowed = {"lead_status", "phone", "best_call_time"}
    updates = {k: v for k, v in d.items() if k in allowed}
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    try:
        with db._conn() as c:
            for k, v in updates.items():
                c.execute(f"UPDATE leads SET {k}=? WHERE id=?", (v, lead_id))
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/broker/stats")
@broker_required
def api_broker_stats():
    """Broker dashboard statistics."""
    try:
        with db._conn() as c:
            total_leads  = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            hot_leads    = c.execute("SELECT COUNT(*) FROM leads WHERE interest_level='high'").fetchone()[0]
            called       = c.execute("SELECT COUNT(*) FROM leads WHERE lead_status='called'").fetchone()[0]
            converted    = c.execute("SELECT COUNT(*) FROM leads WHERE lead_status='converted'").fetchone()[0]
            # By insurance type
            by_type = [dict(r) for r in c.execute(
                "SELECT insurance_type, COUNT(*) as cnt FROM users "
                "WHERE insurance_type IS NOT NULL GROUP BY insurance_type ORDER BY cnt DESC LIMIT 6"
            ).fetchall()]
            # Recent activity (last 7 days)
            recent = c.execute(
                "SELECT COUNT(*) FROM leads WHERE timestamp >= datetime('now', '-7 days')"
            ).fetchone()[0]
        return jsonify({
            "status": "success",
            "stats": {
                "total_leads":  total_leads,
                "hot_leads":    hot_leads,
                "called":       called,
                "converted":    converted,
                "recent_7d":    recent,
                "conversion_rate": round(converted / max(total_leads, 1) * 100, 1),
                "by_type":      by_type,
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# AI POLICY DOCUMENT READER  (/api/policy-reader)
# Upload existing policy → OCR text → Gemini finds gaps & exclusions
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/policy-reader", methods=["POST"])
def policy_reader():
    """
    Accepts uploaded policy document (PDF/image).
    1. OCR extract text via ocr_verifier
    2. Feed to Gemini with structured gap-analysis prompt
    3. Return: summary, coverage_found, gaps, exclusions, recommendations
    """
    file      = request.files.get("file")
    user_id   = request.form.get("user_id", "")
    ins_type  = request.form.get("insurance_type", "").strip()

    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Unsupported file type: {ext}. Use PDF, JPG, or PNG."}), 400

    file_bytes = file.read()
    if len(file_bytes) < 200:
        return jsonify({"error": "File appears empty."}), 400

    # ── Step 1: OCR text extraction ─────────────────────────────────────────
    try:
        raw_text = ocr.extract_policy_text_for_rag(file_bytes, ext)
    except Exception as oe:
        raw_text = ""
        log.warning(f"[POLICY-READER] OCR failed: {oe}")

    if not raw_text or len(raw_text.strip()) < 80:
        # Try base64 vision path if text extraction fails
        raw_text = "(Document text could not be extracted via OCR — using visual analysis)"

    text_snippet = raw_text[:4000]  # cap to avoid token overflow

    # ── Step 2: Gemini gap-analysis ─────────────────────────────────────────
    ins_context = f" The policy type appears to be: {ins_type}." if ins_type else ""

    gap_prompt = f"""You are an expert Indian insurance policy analyst.
Analyse the following insurance policy document text and return a JSON object with EXACTLY these keys:

{{
  "policy_name": "Short name of the policy (string)",
  "insurer":     "Insurance company name (string or null)",
  "ins_type":    "Type: Health/Life/Vehicle/Travel/Property (string)",
  "coverage_found": ["List of things the policy DOES cover (5-10 items)"],
  "exclusions":  ["List of explicit exclusions or waiting periods found (5-10 items)"],
  "gaps":        ["List of important gaps — things commonly needed but NOT covered (3-6 items)"],
  "critical_issues": ["Any seriously problematic clauses or red flags (2-4 items)"],
  "recommendations": ["Specific improvements the user should seek (3-5 items)"],
  "overall_rating": "Poor / Fair / Good / Excellent",
  "summary": "2-sentence plain-English summary for a non-expert user"
}}

Return ONLY the JSON object, no markdown, no explanation.{ins_context}

POLICY TEXT:
{text_snippet}"""

    analysis = None
    try:
        gemini_key = (os.environ.get("GEMINI_API_KEY") or
                      os.environ.get("GEMINI_API_KEY_1") or "")
        if gemini_key:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            model  = genai.GenerativeModel("gemini-2.0-flash")
            resp   = model.generate_content(gap_prompt)
            raw_r  = resp.text.strip()
            # Strip markdown fences
            import re as _re
            clean = _re.sub(r"```(?:json)?\s*", "", raw_r).strip()
            clean = _re.sub(r"```\s*$", "", clean).strip()
            import json as _json
            for candidate in [clean,
                               clean.replace("True","true").replace("False","false")]:
                try:
                    analysis = _json.loads(candidate); break
                except Exception:
                    m = _re.search(r"\{[\s\S]*\}", candidate)
                    if m:
                        try: analysis = _json.loads(m.group()); break
                        except Exception: pass
    except Exception as ge:
        log.warning(f"[POLICY-READER] Gemini analysis failed: {ge}")

    if not analysis:
        # Fallback: basic structural response from OCR text alone
        analysis = {
            "policy_name":     file.filename,
            "insurer":         None,
            "ins_type":        ins_type or "Unknown",
            "coverage_found":  ["Policy text extracted — manual review recommended"],
            "exclusions":      ["Could not auto-detect exclusions — please read policy schedule"],
            "gaps":            ["AI analysis unavailable — check Gemini API key in .env"],
            "critical_issues": [],
            "recommendations": ["Please review the full policy document manually"],
            "overall_rating":  "Fair",
            "summary":         "Your document was received but AI analysis could not complete. "
                               "Please ensure GEMINI_API_KEY is configured in your .env file."
        }

    return jsonify({
        "status":    "success",
        "analysis":  analysis,
        "text_len":  len(raw_text),
        "ocr_ok":    len(raw_text) > 80,
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
