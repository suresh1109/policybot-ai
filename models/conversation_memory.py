"""
conversation_memory.py — Conversation Memory & Context Awareness for PolicyBot v14
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Provides:
  - ConversationMemory:   per-session in-memory store (user_profile + completed_steps + last_question)
  - MemoryManager:        singleton that manages multiple sessions
  - step_for_field():     maps profile fields → their corresponding step names
  - mark_steps_from_profile(): automatically marks completed steps given current profile
  - get_next_missing_step(): returns next step still needing data
"""

import time
from typing import Optional

# ── Step definitions (matches ConversationEngine.STEPS) ──────────────────────
ALL_STEPS = [
    "data_extraction",          # name / age / insurance_type smart-extracted
    "government_id_verification",
    "location_confirmation",
    "coverage_selection",
    "family_member_collection",
    "medical_condition_check",
    "medical_report_upload",
    "budget_collection",
    "review_details",
    "fraud_detection",
    "risk_scoring",
    "premium_prediction",
    "recommendation_generation",
    "report_generation",
]

# Map each step to the profile field(s) that indicate it is complete
STEP_COMPLETION_FIELDS = {
    "data_extraction":           ["name", "age"],
    "government_id_verification":["gov_id_verified"],
    "location_confirmation":     ["city"],
    "coverage_selection":        ["coverage_type"],
    "family_member_collection":  ["family_members_json"],
    "medical_condition_check":   ["medical_conditions_status"],
    "medical_report_upload":     ["medical_report_uploaded"],
    "budget_collection":         ["budget_range"],
    "review_details":            ["review_confirmed"],
    "fraud_detection":           ["fraud_status"],
    "risk_scoring":              ["risk_score"],
    "premium_prediction":        ["premium_prediction"],
    "recommendation_generation": ["selected_plan"],
    "report_generation":         [],          # generated on demand
}

# Map profile fields → the step they belong to
FIELD_TO_STEP = {
    "name":                    "data_extraction",
    "age":                     "data_extraction",
    "insurance_type":          "data_extraction",
    "gov_id_verified":         "government_id_verification",
    "city":                    "location_confirmation",
    "coverage_type":           "coverage_selection",
    "family_members_json":     "family_member_collection",
    "medical_conditions_status":"medical_condition_check",
    "medical_conditions":      "medical_condition_check",
    "medical_report_uploaded": "medical_report_upload",
    "budget_range":            "budget_collection",
    "review_confirmed":        "review_details",
    "fraud_status":            "fraud_detection",
    "risk_score":              "risk_scoring",
    "premium_prediction":      "premium_prediction",
    "selected_plan":           "recommendation_generation",
}


def step_for_field(field: str) -> Optional[str]:
    """Return the step name that a profile field belongs to, or None."""
    return FIELD_TO_STEP.get(field)


class ConversationMemory:
    """
    Per-session memory store.

    Attributes
    ----------
    user_profile    : dict   — all collected profile fields
    conversation_state : str — current onboarding_stage (mirrors DB)
    completed_steps : list   — ordered list of completed step names
    last_question   : str    — the last question asked by the bot
    created_at      : float  — unix timestamp of session creation
    updated_at      : float  — unix timestamp of last update
    """

    def __init__(self, user_id: str):
        self.user_id            = user_id
        self.user_profile: dict = {}
        self.conversation_state: str = "insurance_type"
        self.completed_steps: list  = []
        self.last_question: Optional[str] = None
        self.created_at   = time.time()
        self.updated_at   = time.time()

    # ── Profile updates ───────────────────────────────────────────────────────
    def update_profile(self, fields: dict):
        """Merge new fields into user_profile and auto-mark completed steps."""
        self.user_profile.update(fields)
        self.updated_at = time.time()
        for field in fields:
            step = FIELD_TO_STEP.get(field)
            if step and step not in self.completed_steps:
                self.completed_steps.append(step)

    def set_state(self, stage: str):
        self.conversation_state = stage
        self.updated_at = time.time()

    def set_last_question(self, question: str):
        self.last_question = question
        self.updated_at = time.time()

    # ── Step tracking ─────────────────────────────────────────────────────────
    def mark_step_complete(self, step: str):
        if step in ALL_STEPS and step not in self.completed_steps:
            self.completed_steps.append(step)
            self.updated_at = time.time()

    def is_step_complete(self, step: str) -> bool:
        return step in self.completed_steps

    def mark_steps_from_profile(self):
        """Scan current user_profile and mark all appropriate steps complete."""
        for step, fields in STEP_COMPLETION_FIELDS.items():
            if not fields:
                continue
            if all(self.user_profile.get(f) for f in fields):
                if step not in self.completed_steps:
                    self.completed_steps.append(step)

    # ── Skip logic ────────────────────────────────────────────────────────────
    def should_skip_question(self, field: str) -> bool:
        """
        Returns True if the field is already known — the bot should NOT ask again.
        Example: if age exists in user_profile → skip age question.
        """
        val = self.user_profile.get(field)
        if val is None:
            return False
        if isinstance(val, str):
            return val.strip() != ""
        return bool(val)

    def should_skip_step(self, step: str) -> bool:
        """Returns True if the step is already in completed_steps."""
        return step in self.completed_steps

    # ── Next missing step ─────────────────────────────────────────────────────
    def get_next_incomplete_step(self) -> Optional[str]:
        """Return the first step in ALL_STEPS not yet completed."""
        for step in ALL_STEPS:
            if step not in self.completed_steps:
                return step
        return None

    # ── Context summary (for AI system prompt injection) ──────────────────────
    def get_context_summary(self) -> str:
        """
        Returns a concise text block that can be injected into the AI prompt
        so the model knows exactly what has already been collected.
        """
        p = self.user_profile
        lines = ["SESSION MEMORY (already known — DO NOT ask again):"]
        if p.get("name"):           lines.append(f"  name            = {p['name']}")
        if p.get("age"):            lines.append(f"  age             = {p['age']}")
        if p.get("insurance_type"): lines.append(f"  insurance_type  = {p['insurance_type']}")
        if p.get("city"):           lines.append(f"  city            = {p['city']}")
        if p.get("coverage_type"):  lines.append(f"  coverage_type   = {p['coverage_type']}")
        if p.get("medical_conditions"): lines.append(f"  medical         = {p['medical_conditions']}")
        if p.get("budget_range"):   lines.append(f"  budget          = {p['budget_range']}")
        if p.get("gender"):         lines.append(f"  gender          = {p['gender']}")
        if p.get("gov_id_verified"):lines.append(f"  id_verified     = yes")
        if self.completed_steps:
            lines.append(f"  completed_steps = {', '.join(self.completed_steps)}")
        if self.last_question:
            lines.append(f"  last_question   = {self.last_question}")
        lines.append(f"  current_stage   = {self.conversation_state}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "user_id":            self.user_id,
            "user_profile":       dict(self.user_profile),
            "conversation_state": self.conversation_state,
            "completed_steps":    list(self.completed_steps),
            "last_question":      self.last_question,
            "created_at":         self.created_at,
            "updated_at":         self.updated_at,
        }

    def __repr__(self):
        return (f"<ConversationMemory user={self.user_id} "
                f"stage={self.conversation_state} "
                f"steps={len(self.completed_steps)}/{len(ALL_STEPS)}>")


class MemoryManager:
    """
    Singleton-style registry of ConversationMemory objects, keyed by user_id.
    Automatically expires sessions older than TTL_HOURS (default 4h).
    """
    TTL_SECONDS = 4 * 3600  # 4 hours

    def __init__(self):
        self._sessions: dict[str, ConversationMemory] = {}

    def get(self, user_id: str) -> ConversationMemory:
        """Get or create a ConversationMemory for the given user_id."""
        self._evict_expired()
        if user_id not in self._sessions:
            self._sessions[user_id] = ConversationMemory(user_id)
        return self._sessions[user_id]

    def reset(self, user_id: str) -> ConversationMemory:
        """Destroy and recreate the session memory for a user."""
        mem = ConversationMemory(user_id)
        self._sessions[user_id] = mem
        return mem

    def sync_from_profile(self, user_id: str, profile: dict) -> ConversationMemory:
        """
        Sync an existing DB profile into memory.
        Call this at the start of each request so memory stays consistent with DB.
        """
        mem = self.get(user_id)
        # Merge all profile fields (don't overwrite with empty values)
        for k, v in profile.items():
            if v is not None and k != "onboarding_stage":
                mem.user_profile[k] = v
        if profile.get("onboarding_stage"):
            mem.conversation_state = profile["onboarding_stage"]
        mem.mark_steps_from_profile()
        return mem

    def update_from_extracted(self, user_id: str, extracted: dict):
        """Called whenever the conversation engine extracts new fields."""
        mem = self.get(user_id)
        mem.update_profile(extracted)

    def advance_stage(self, user_id: str, new_stage: str, last_question: str = ""):
        """Called when the conversation engine moves to a new stage."""
        mem = self.get(user_id)
        mem.set_state(new_stage)
        if last_question:
            mem.set_last_question(last_question)

    def get_context_summary(self, user_id: str) -> str:
        return self.get(user_id).get_context_summary()

    def _evict_expired(self):
        now = time.time()
        expired = [uid for uid, m in self._sessions.items()
                   if (now - m.updated_at) > self.TTL_SECONDS]
        for uid in expired:
            del self._sessions[uid]

    def stats(self) -> dict:
        return {
            "active_sessions": len(self._sessions),
            "users": list(self._sessions.keys()),
        }


# ── Global singleton ─────────────────────────────────────────────────────────
memory_manager = MemoryManager()
