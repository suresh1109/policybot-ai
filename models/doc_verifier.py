"""
DocumentVerifier v6 — Age + Name Verification (COMPLETE REWRITE)
CHANGES IN v6:
  - VISION_PROMPT now REQUIRES name extraction (was wrongly blocking it)
  - verify() checks BOTH age AND name — clear mismatch message for each
  - Name comparison: word-level overlap (handles middle names, initials)
  - _result() now includes 'doc_name' field for frontend display
  - Detailed success: "Name: X verified | Age: Born YYYY confirmed"
  - All fail paths show exactly what failed (name vs age vs quality)
"""
import os, base64, re, datetime, json, logging

log = logging.getLogger("DocVerifier")


class DocumentVerifier:

    VISION_PROMPT = """You are a document verification AI. Analyze this Indian Government ID image carefully.

EXTRACT THESE FIELDS ACCURATELY:

1. Is this a valid Indian Government ID?
   Valid types: Aadhaar Card, PAN Card, Driving License, Passport, Voter ID

2. Image quality — can all text be read clearly?

3. FULL NAME printed on the document (READ CAREFULLY — required for identity verification)
   - Aadhaar: Name is the large bold text line just below the top blue header/logo
   - PAN: Name in CAPITAL LETTERS in the middle of the card
   - Driving License: Look for "Name:" label — the name follows
   - Passport: Name in the Machine Readable Zone at the bottom (P<IND...)
   - Voter ID: Look for "ELECTOR'S NAME:" or "Name:" label

4. Date of Birth (DOB) — usually labeled "DOB:", "Date of Birth:", "D.O.B:"
   Format usually DD/MM/YYYY

5. Exact ID document type

PRIVACY RULE:
- DO NOT output Aadhaar number, PAN number, DL number, or any numeric ID number
- DO output full name and DOB — needed for identity verification

IMAGE QUALITY VALUES:
  good=sharp well-lit fully visible | blurry=out of focus | dark=too dim
  cropped=edges cut off | incomplete=part missing | unreadable=cannot read

OUTPUT ONLY VALID JSON — NO markdown fences, NO explanation:
{
  "is_valid_id": true or false,
  "id_type": "Aadhaar" | "PAN" | "Driving License" | "Passport" | "Voter ID" | "Unknown" | "Not an ID",
  "image_quality": "good" | "blurry" | "dark" | "cropped" | "incomplete" | "unreadable",
  "name_visible": true or false,
  "name": "Full Name As On Document" or null,
  "dob_visible": true or false,
  "dob": "DD/MM/YYYY" or null,
  "dob_confidence": "high" | "medium" | "low" | "none",
  "notes": "brief note — no ID numbers"
}"""

    BAD_QUALITIES = {"blurry", "dark", "cropped", "incomplete", "unreadable"}
    QUALITY_TIPS  = {
        "blurry":     "📸 Tip: Hold steady and tap screen to focus before capturing.",
        "dark":       "💡 Tip: Move to a brighter area or use your phone flashlight.",
        "cropped":    "📐 Tip: Make sure all 4 corners of the ID are in the frame.",
        "incomplete": "📋 Tip: Ensure the complete document is visible.",
        "unreadable": "🔍 Tip: Place document flat and photograph from directly above.",
    }

    def __init__(self, gemini_manager):
        self.gemini = gemini_manager

    # ════════════════════════════════════════════════════════
    # MAIN VERIFY  — checks both NAME and AGE
    # ════════════════════════════════════════════════════════
    def verify(self, file_path, file_bytes, file_ext, doc_type,
               stated_age, user_id, session_id="", stated_name=""):
        sid = session_id or user_id
        print(f"[CONSOLE] Document uploaded for {sid}")
        log.info(f"[UPLOAD] sid={sid} ext={file_ext} size={len(file_bytes)}")
        log.info(f"[VERIFY] stated_age={stated_age}  stated_name='{stated_name}'")

        b64      = base64.b64encode(file_bytes).decode("utf-8")
        mime     = self._mime(file_ext)
        analysis = self._call_vision(b64, mime, file_ext)

        if analysis is None:
            return self._result("api_error", False,
                "😊 Our AI verification is temporarily busy. Please try again in a moment.",
                options=["Upload Document Again", "Continue Without Verification"])

        is_valid   = analysis.get("is_valid_id", False)
        id_type    = analysis.get("id_type", "Unknown")
        quality    = analysis.get("image_quality", "unreadable")
        name_vis   = analysis.get("name_visible", False)
        name_on_id = (analysis.get("name") or "").strip()
        dob_vis    = analysis.get("dob_visible", False)
        dob_raw    = analysis.get("dob")
        dob_conf   = analysis.get("dob_confidence", "none")
        notes      = analysis.get("notes", "")

        log.info(f"[VERIFY] valid={is_valid} id={id_type} quality={quality} "
                 f"name_vis={name_vis} name='{name_on_id}' "
                 f"dob_vis={dob_vis} dob='{dob_raw}' conf={dob_conf}")

        # ── 1. Valid ID? ──────────────────────────────────────────────────
        if not is_valid or id_type in ("Unknown", "Not an ID", ""):
            return self._result("not_valid_id", False,
                "❌ This doesn't look like a valid Indian Government ID.\n"
                "Please upload Aadhaar, PAN, Driving License, Passport, or Voter ID.",
                quality=quality, id_type=id_type,
                options=["Upload Correct Document", "Continue Without Verification"])

        # ── 2. Image quality ─────────────────────────────────────────────
        if quality in self.BAD_QUALITIES:
            tip = self.QUALITY_TIPS.get(quality, "Please try a clearer photo.")
            return self._result("low_quality", False,
                f"❌ Your {id_type} image is **{quality}** and hard to read.\n{tip}",
                quality=quality, id_type=id_type,
                options=["Upload Clear Document", "Continue Without Verification"])

        # ── 3. NAME VERIFICATION ─────────────────────────────────────────
        name_ok    = False   # True only when name match confirmed
        name_badge = ""      # line added to success message

        if stated_name and name_vis and name_on_id:
            name_ok, score = self._compare_names(name_on_id, stated_name)
            log.info(f"[NAME] id='{name_on_id}'  stated='{stated_name}'  "
                     f"match={name_ok}  score={score}")
            if name_ok:
                name_badge = f"\n✅ **Name verified:** {name_on_id}"
            else:
                # Hard fail — name clearly doesn't match
                return self._result("name_mismatch", False,
                    f"⚠️ **Name mismatch detected on your {id_type}!**\n\n"
                    f"👤 Name on document: **{name_on_id}**\n"
                    f"👤 Name you gave me: **{stated_name}**\n\n"
                    "These don't match. Please upload an ID with your correct name.",
                    quality=quality, id_type=id_type, doc_name=name_on_id,
                    options=["Upload Different Document", "Continue Without Verification"])
        elif stated_name and (not name_vis or not name_on_id):
            log.info(f"[NAME] Name not visible on ID — skipping name check")
            name_badge = "\n⚠️ Name not readable on this ID"

        # ── 4. AGE VERIFICATION ──────────────────────────────────────────
        if not dob_vis or dob_conf in ("none", "low") or not dob_raw:
            if name_ok:
                # Name passed but DOB missing
                return self._result("name_ok_dob_missing", False,
                    f"✅ Name verified on your {id_type} ({name_on_id}), "
                    "but the Date of Birth wasn't readable clearly.\n"
                    "Please upload a clearer photo.",
                    quality=quality, id_type=id_type, doc_name=name_on_id,
                    options=["Upload Clear Document", "Continue Without Verification"])
            return self._result("dob_not_found", False,
                f"❌ Couldn't read the Date of Birth on your {id_type}.\n"
                "Please upload a clearer photo or try a different ID.",
                quality=quality, id_type=id_type,
                options=["Upload Clear Document", "Continue Without Verification"])

        age_ok, dob_year = self._check_age(dob_raw, stated_age)

        if age_ok is None:
            return self._result("dob_parse_error", False,
                f"❌ Found a date on your {id_type} but couldn't compute age from it.\n"
                "Please try a different ID.",
                quality=quality, id_type=id_type,
                options=["Upload Clear Document", "Continue Without Verification"])

        # ── 5. FINAL RESULT ──────────────────────────────────────────────
        if age_ok:
            age_badge = f"\n✅ **Age verified:** Born {dob_year}" if dob_year else ""
            return self._result("verified", True,
                f"✅ **{id_type} verified successfully!** 🎉"
                f"{name_badge}"
                f"{age_badge}"
                "\n\nYour identity is confirmed — let's continue! 👍",
                quality=quality, id_type=id_type,
                dob_year=dob_year, doc_name=name_on_id, notes=notes)
        else:
            return self._result("age_mismatch", False,
                f"⚠️ **Age mismatch on your {id_type}!**\n\n"
                f"🎂 Document shows birth year: **{dob_year}**\n"
                f"🎂 Age you told me: **{stated_age} years**\n\n"
                "These don't match. Please upload a different ID or continue without verification.",
                quality=quality, id_type=id_type, doc_name=name_on_id,
                options=["Upload Different Document", "Continue Without Verification"])

    # ════════════════════════════════════════════════════════
    # NAME COMPARISON
    # ════════════════════════════════════════════════════════
    def _compare_names(self, id_name: str, stated_name: str):
        """Word-level overlap. Returns (matched, score). Score = # matching words."""
        STOP = {"mr", "mrs", "ms", "dr", "shri", "smt", "kumari", "s/o", "d/o", "w/o", "ko"}

        def words(s):
            s = re.sub(r"[^a-z\s]", "", s.lower())
            return {w for w in s.split() if len(w) > 1 and w not in STOP}

        overlap = words(id_name) & words(stated_name)
        return (len(overlap) >= 1, len(overlap))

    # ════════════════════════════════════════════════════════
    # GEMINI VISION CALL
    # ════════════════════════════════════════════════════════
    def _call_vision(self, b64, mime, file_ext):
        try:
            import google.generativeai as genai
        except ImportError:
            log.error("[VERIFY] google-generativeai not installed")
            return None

        keys = []
        k = os.environ.get("GEMINI_API_KEY", "").strip()
        if k:
            keys.append(k)
        for i in range(1, 6):
            k = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
            if k and k not in keys:
                keys.append(k)

        if not keys:
            log.error("[VERIFY] No GEMINI_API_KEY found in environment")
            return None

        log.info(f"[VERIFY] {len(keys)} key(s) available — calling Gemini Vision")

        for attempt, key in enumerate(keys):
            try:
                genai.configure(api_key=key)
                model   = genai.GenerativeModel("gemini-2.0-flash")
                mt      = "application/pdf" if file_ext.lower() == ".pdf" else mime
                content = [{"inline_data": {"mime_type": mt, "data": b64}},
                           self.VISION_PROMPT]
                resp = model.generate_content(content)
                raw  = resp.text.strip()
                log.debug(f"[VERIFY] Raw (key {attempt+1}): {raw[:400]}")
                return self._parse_json(raw)

            except Exception as e:
                err = str(e).lower()
                if any(x in err for x in ["quota", "429", "resource_exhausted", "rate_limit"]):
                    log.warning(f"[VERIFY] Key {attempt+1} quota exceeded — rotating")
                elif any(x in err for x in ["api_key", "permission", "invalid", "credentials"]):
                    log.warning(f"[VERIFY] Key {attempt+1} invalid — rotating")
                elif any(x in err for x in ["timeout", "deadline", "connection", "unavailable"]):
                    log.warning(f"[VERIFY] Key {attempt+1} timeout — rotating")
                else:
                    log.error(f"[VERIFY] Unexpected error key {attempt+1}: {e}")
                    return None   # non-recoverable

        log.error("[VERIFY] All API keys exhausted")
        return None

    # ════════════════════════════════════════════════════════
    # JSON PARSER
    # ════════════════════════════════════════════════════════
    def _parse_json(self, raw):
        if not raw:
            return None
        text = re.sub(r"```(?:json)?\s*", "", raw).strip()
        text = re.sub(r"```\s*$", "", text).strip()

        for candidate in [text,
                          text.replace("True","true").replace("False","false").replace("None","null")]:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            m = re.search(r"\{[\s\S]*\}", candidate)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        log.warning(f"[VERIFY] Could not parse JSON: {raw[:300]}")
        return None

    # ════════════════════════════════════════════════════════
    # AGE CHECK  ±2 year tolerance
    # ════════════════════════════════════════════════════════
    def _check_age(self, dob_str, stated_age):
        if not dob_str or stated_age is None:
            return None, ""
        dob = None
        for fmt in ["%d/%m/%Y","%d-%m-%Y","%Y-%m-%d","%d/%m/%y","%d.%m.%Y",
                    "%m/%d/%Y","%d %B %Y","%d %b %Y","%B %d, %Y","%b %d, %Y",
                    "%Y/%m/%d","%d %m %Y"]:
            try:
                dob = datetime.datetime.strptime(dob_str.strip(), fmt).date(); break
            except ValueError:
                continue

        if not dob:
            ym = re.search(r"\b(19\d{2}|20[0-2]\d)\b", dob_str)
            if ym:
                birth_year = int(ym.group())
                calc_age   = datetime.date.today().year - birth_year
                try:
                    return abs(calc_age - int(stated_age)) <= 2, str(birth_year)
                except (ValueError, TypeError):
                    return None, ""
            return None, ""

        calc_age = (datetime.date.today() - dob).days // 365
        try:
            return abs(calc_age - int(stated_age)) <= 2, str(dob.year)
        except (ValueError, TypeError):
            return None, ""

    # ════════════════════════════════════════════════════════
    # MIME / RESULT / CLEANUP
    # ════════════════════════════════════════════════════════
    def _mime(self, ext):
        return {".jpg":"image/jpeg",".jpeg":"image/jpeg",
                ".png":"image/png",".webp":"image/webp",
                ".gif":"image/gif",".bmp":"image/bmp",
                ".tiff":"image/tiff",".tif":"image/tiff",
                ".pdf":"application/pdf"}.get(ext.lower(),"image/jpeg")

    def _result(self, status, verified, message,
                quality="", id_type="", dob_year="",
                doc_name="", notes="", options=None):
        return {
            "status":         status,
            "verified":       verified,
            "message":        message,
            "reply":          message,       # alias used by frontend
            "quality":        quality,
            "doc_type_found": id_type,
            "doc_name":       doc_name,      # name found on document
            "dob_year":       dob_year,
            "notes":          notes,
            "options":        options or [],
            "option_type":    "radio" if options else "none",
        }

    @staticmethod
    def delete_file(fp):
        try:
            if fp and os.path.exists(fp):
                os.remove(fp); return True
        except Exception as e:
            log.error(f"[CLEANUP] {e}")
        return False

    @staticmethod
    def delete_user_uploads(user_id, db):
        docs    = db.get_user_documents(user_id)
        deleted = sum(1 for d in docs if DocumentVerifier.delete_file(d.get("file_path","")))
        db.delete_user_documents(user_id)
        log.info(f"[CLEANUP] Deleted {deleted} files for {user_id}")
        return deleted