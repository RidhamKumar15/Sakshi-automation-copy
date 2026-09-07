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
    - >= 30: Auto-apply
    - 15 - 29: Review Queue
    - < 15: Skip
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
            "auto_apply_min_score": 30,
            "review_queue_min_score": 15
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
        required_skills = job_data.get("required_skills") or []
        preferred_skills = job_data.get("preferred_skills") or []
        experience_required = job_data.get("experience_required") or ""
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
        for s in req_skills + pref_skills:
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
        # e.g., "0-2 years", "1-3 years", "minimum 5 years", "3+ years"
        years_matches = re.findall(r'(\d+)(?:\s*(?:-|to|\+)\s*(\d+))?\s*(?:years?|yrs?)', combined)
        min_years = 0
        if years_matches:
            try:
                # Find smallest minimum year mentioned
                min_years = min(int(m[0]) for m in years_matches)
            except Exception:
                min_years = 0

        if min_years == 0 or "entry level" in combined or "graduate" in combined or "fresher" in combined or "junior" in combined:
            return float(max_pts), {"min_years_required": min_years, "level": "Entry / Junior"}
        elif min_years <= 2:
            return float(max_pts), {"min_years_required": min_years, "level": "0-2 Years"}
        elif min_years == 3:
            return round(max_pts * 0.6, 1), {"min_years_required": min_years, "level": "3 Years"}
        else:
            return 0.0, {"min_years_required": min_years, "level": "Senior / Unreasonable (>3 yrs)"}

    def _score_role_relevance(self, title: str) -> Tuple[float, Dict[str, Any]]:
        max_pts = self.weights["role_relevance"]
        title_lower = title.lower()

        for neg in self.negative_role_keywords:
            if neg in title_lower:
                return 0.0, {"reason": f"Disqualifying title keyword: {neg}"}

        # Exact or close target role match
        for target in self.target_roles:
            if target in title_lower:
                return float(max_pts), {"matched_target_role": target}

        # Partial software & tech keywords
        dev_keywords = [
            "software", "developer", "engineer", "backend", "full stack", "fullstack", "frontend",
            "java", "python", "programmer", "sde", "ai", "ml", "data", "web", "react", "node",
            "intern", "trainee", "associate", "tech", "database", "sql", "coder", "it", "systems",
            "cloud", "devops", "application", "full-stack"
        ]
        matches = [k for k in dev_keywords if k in title_lower]
        if len(matches) >= 2:
            return round(max_pts * 0.9, 1), {"partial_matches": matches}
        elif len(matches) == 1:
            return round(max_pts * 0.6, 1), {"partial_matches": matches}

        return 0.0, {"reason": "Not a software/tech developer role"}

    def _score_location(self, location: str, remote_status: str, description: str) -> Tuple[float, Dict[str, Any]]:
        max_pts = self.weights["location_preference"]
        loc_str = f"{location} {remote_status} {description[:300]}".lower()

        if "remote" in loc_str or "work from home" in loc_str or "anywhere" in loc_str:
            return float(max_pts), {"matched": "Remote"}

        for target_loc in self.target_locations:
            if target_loc in loc_str:
                return float(max_pts), {"matched": target_loc}

        if "india" in loc_str:
            return float(max_pts), {"matched": "India"}

        # Outside preferred location or unspecified
        if location:
            return round(max_pts * 0.3, 1), {"matched": f"Other location: {location}"}

        return round(max_pts * 0.5, 1), {"matched": "Unspecified location"}

    def _score_eligibility(self, description: str) -> Tuple[float, Dict[str, Any]]:
        max_pts = self.weights["eligibility"]
        desc_lower = description.lower()

        # Check for visa restriction warnings (e.g. "US Citizen only", "requires active security clearance", "only local citizens")
        if "security clearance required" in desc_lower or "us citizens only" in desc_lower or "green card only" in desc_lower:
            return 0.0, {"reason": "Disqualifying legal/citizenship requirement"}

        # Candidate has CS degree, work authorization in India, immediately available
        score = float(max_pts)
        details = {"authorized": True, "education_match": True}
        return score, details
