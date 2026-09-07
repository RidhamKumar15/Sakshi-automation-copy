import re
import json
from typing import Dict, Any, List, Tuple


class JobScorer:
    """
    Evaluates job openings against candidate profile and preferences using the 0-100 scoring system:
    - Skill match: 30 points
    - Experience match: 25 points
    - Role relevance: 25 points
    - Location/work preference: 10 points
    - Eligibility and other requirements: 10 points
    
    Thresholds:
    - >= 70: Auto-apply
    - 55 - 69: Review Queue
    - < 55: Skip
    """

    def __init__(self, candidate_profile: Dict[str, Any], settings: Dict[str, Any]):
        self.profile = candidate_profile
        self.settings = settings
        self.weights = settings.get("scoring", {}).get("weights", {
            "skill_match": 30,
            "experience_match": 25,
            "role_relevance": 25,
            "location_preference": 10,
            "eligibility": 10
        })
        self.thresholds = settings.get("scoring", {}).get("thresholds", {
            "auto_apply_min_score": 70,
            "review_queue_min_score": 55
        })

        # Collect all candidate skill keywords (lowercased)
        tech = self.profile.get("technical_skills", {})
        self.candidate_skills = set()
        for cat, skills in tech.items():
            if isinstance(skills, list):
                for s in skills:
                    self.candidate_skills.add(s.lower().strip())

        self.target_roles = [r.lower().strip() for r in self.settings.get("search_criteria", {}).get("target_roles", [])]
        self.target_locations = [l.lower().strip() for l in self.settings.get("search_criteria", {}).get("target_locations", [])]

        # Negative role keywords
        self.negative_role_keywords = [
            "senior", "sr.", "sr ", "lead", "principal", "staff", "architect",
            "manager", "director", "head of", "vp", "vice president",
            "sales", "marketing", "recruiter", "account executive", "devops lead"
        ]

    def evaluate(self, job_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculates score breakdown and final recommendation for a given job.
        """
        title = (job_data.get("title") or "").strip()
        description = (job_data.get("description") or "").strip()
        required_skills = job_data.get("required_skills") or job_data.get("skills") or []
        preferred_skills = job_data.get("preferred_skills") or []
        experience_required = job_data.get("experience_required") or job_data.get("experience") or ""
        location = (job_data.get("location") or "").strip()
        remote_status = (job_data.get("remote_status") or "").strip()

        # 1. Skill Match (30 pts)
        skill_score, skill_details = self._score_skills(description, required_skills, preferred_skills)

        # 2. Experience Match (25 pts)
        exp_score, exp_details = self._score_experience(title, description, experience_required)

        # 3. Role Relevance (25 pts)
        role_score, role_details = self._score_role_relevance(title)

        # 4. Location/Work Preference (10 pts)
        loc_score, loc_details = self._score_location(location, remote_status, description)

        # 5. Eligibility & Requirements (10 pts)
        elig_score, elig_details = self._score_eligibility(description)

        total_score = round(skill_score + exp_score + role_score + loc_score + elig_score, 1)

        # Determine Decision
        if total_score >= self.thresholds["auto_apply_min_score"]:
            decision = "AUTO_APPLY"
            status = "Applying"
        elif total_score >= self.thresholds["review_queue_min_score"]:
            decision = "REVIEW_QUEUE"
            status = "Under Review"
        else:
            decision = "SKIP"
            status = "Skipped"

        return {
            "total_score": total_score,
            "decision": decision,
            "status": status,
            "breakdown": {
                "skill_match": {"score": skill_score, "max": self.weights["skill_match"], "details": skill_details},
                "experience_match": {"score": exp_score, "max": self.weights["experience_match"], "details": exp_details},
                "role_relevance": {"score": role_score, "max": self.weights["role_relevance"], "details": role_details},
                "location_preference": {"score": loc_score, "max": self.weights["location_preference"], "details": loc_details},
                "eligibility": {"score": elig_score, "max": self.weights["eligibility"], "details": elig_details}
            }
        }

    def _score_skills(self, description: str, req_skills: List[str], pref_skills: List[str]) -> Tuple[float, Dict[str, Any]]:
        max_pts = self.weights["skill_match"]
        text_lower = description.lower()

        # Combine explicit skills and extracted skills from description
        found_job_skills = set()
        for s in (req_skills or []) + (pref_skills or []):
            if s:
                found_job_skills.add(s.lower().strip())

        # Also search candidate skills in description text
        for s in self.candidate_skills:
            # Word boundary regex check
            pattern = r'\b' + re.escape(s) + r'\b'
            if re.search(pattern, text_lower):
                found_job_skills.add(s)

        if not found_job_skills:
            # If no specific skills mentioned in job card, give baseline 15 pts if it's general software role
            return round(max_pts * 0.5, 1), {"matched": [], "total_detected": 0}

        matched = [s for s in found_job_skills if s in self.candidate_skills]
        ratio = len(matched) / max(len(found_job_skills), 1)
        score = min(max_pts, round(max_pts * min(1.0, ratio * 1.3), 1))

        return score, {"matched": matched, "total_detected": len(found_job_skills)}

    def _score_experience(self, title: str, description: str, exp_text: str) -> Tuple[float, Dict[str, Any]]:
        max_pts = self.weights["experience_match"]
        combined = f"{title} {exp_text} {description}".lower()

        # Check for senior/lead flags
        for neg in self.negative_role_keywords:
            if neg in title.lower():
                return 0.0, {"reason": f"Title contains disqualifying keyword: {neg}"}

        # Parse years of experience mentioned
        # e.g., "0-2 years", "1-3 years", "minimum 5 years", "3+ years", "0 to 2 Yrs"
        years_matches = re.findall(r'(\d+)(?:\s*(?:-|to|\+)\s*(\d+))?\s*(?:years?|yrs?)', combined)
        min_years = 0
        if years_matches:
            try:
                # Find smallest minimum year mentioned
                min_years = min(int(m[0]) for m in years_matches)
            except Exception:
                min_years = 0

        # Candidate is early-career (0.5 years)
        if "fresher" in combined or "entry level" in combined or "intern" in combined or "graduate" in combined or "trainee" in combined:
            return max_pts, {"min_years": 0, "type": "entry_level_match"}

        if min_years <= 1:
            return max_pts, {"min_years": min_years, "match": "exact_range"}
        elif min_years == 2:
            return round(max_pts * 0.85, 1), {"min_years": 2, "match": "acceptable_range"}
        elif min_years == 3:
            return round(max_pts * 0.4, 1), {"min_years": 3, "match": "stretch_range"}
        else:
            return 0.0, {"min_years": min_years, "match": "too_senior"}

    def _score_role_relevance(self, title: str) -> Tuple[float, Dict[str, Any]]:
        max_pts = self.weights["role_relevance"]
        title_lower = title.lower()

        # Exact or close target match
        for role in self.target_roles:
            if role in title_lower or title_lower in role:
                return max_pts, {"matched_role": role}

        # Partial matching keywords
        core_tokens = ["software", "developer", "engineer", "backend", "python", "java", "full stack", "fullstack", "sde", "trainee", "associate"]
        matches = [t for t in core_tokens if t in title_lower]
        if len(matches) >= 2:
            return round(max_pts * 0.8, 1), {"partial_tokens": matches}
        elif len(matches) == 1:
            return round(max_pts * 0.5, 1), {"partial_tokens": matches}

        return 0.0, {"matched_role": None}

    def _score_location(self, location: str, remote_status: str, description: str) -> Tuple[float, Dict[str, Any]]:
        max_pts = self.weights["location_preference"]
        combined = f"{location} {remote_status} {description}".lower()

        if "remote" in combined or "work from home" in combined or "wfh" in combined or "hybrid" in combined:
            return max_pts, {"preference": "remote/hybrid"}

        for loc in self.target_locations:
            if loc in combined:
                return max_pts, {"preference": loc}

        # Any India location
        if "india" in combined:
            return round(max_pts * 0.8, 1), {"preference": "india"}

        return round(max_pts * 0.5, 1), {"preference": "other"}

    def _score_eligibility(self, description: str) -> Tuple[float, Dict[str, Any]]:
        max_pts = self.weights["eligibility"]
        text_lower = description.lower()

        # Degree match
        degree_keywords = ["b.tech", "btech", "b.e", "be", "bachelor", "computer science", "it", "engineering", "mca", "bca"]
        has_degree = any(k in text_lower for k in degree_keywords)

        # Disqualifiers
        disqualifiers = ["us citizen only", "security clearance required", "must be in us", "eu work authorization"]
        for d in disqualifiers:
            if d in text_lower:
                return 0.0, {"disqualified": d}

        if has_degree or not text_lower:
            return max_pts, {"eligible": True}

        return round(max_pts * 0.8, 1), {"eligible": "assumed"}
