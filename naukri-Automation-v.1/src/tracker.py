import os
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple


class ApplicationTracker:
    """
    Manages persistent application tracking and deduplication for Naukri Automation.
    Maintains:
    - data/tracker.json (structured JSON storage)
    - APPLICATIONS_TRACKER.md (human-readable Markdown table)
    """

    ALLOWED_STATUSES = {
        "Found",
        "Under Review",
        "Applying",
        "Waiting for Input",
        "Submitted",
        "Already Applied",
        "Rejected",
        "Closed",
        "Skipped",
        "Failed"
    }

    def __init__(self, data_file: str = "data/tracker.json", markdown_file: str = "APPLICATIONS_TRACKER.md"):
        self.data_file = data_file
        self.markdown_file = markdown_file
        self.records: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _normalize_key(self, company: str, role: str, url: str, job_id: Optional[str] = None) -> str:
        """
        Creates a canonical deduplication key.
        """
        if job_id and str(job_id).strip():
            return f"id:{str(job_id).strip().lower()}"
        
        # Clean URL (strip query params and hashes for stable matching)
        clean_url = re.sub(r'[?#].*$', '', (url or "").strip().lower())
        if clean_url and any(domain in clean_url for domain in ["naukri.com/job-listings", "naukri.com", "wellfound.com/jobs/", "linkedin.com/jobs/view/"]):
            return f"url:{clean_url}"

        norm_company = re.sub(r'[^a-z0-9]', '', (company or "").lower())
        norm_role = re.sub(r'[^a-z0-9]', '', (role or "").lower())
        return f"comp_role:{norm_company}:{norm_role}"

    def _load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    self.records = json.load(f)
            except Exception as e:
                print(f"[Tracker] Error loading {self.data_file}: {e}")
                self.records = {}
        else:
            self.records = {}
            self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.records, f, indent=2, ensure_ascii=False)
        self._export_markdown()

    def is_duplicate(self, company: str, role: str, url: str = "", job_id: Optional[str] = None) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Checks if a job has already been processed or applied to.
        """
        key = self._normalize_key(company, role, url, job_id)
        if key in self.records:
            return True, self.records[key]

        if url:
            clean_url = re.sub(r'[?#].*$', '', url.strip().lower())
            for rec in self.records.values():
                rec_url = re.sub(r'[?#].*$', '', (rec.get("job_url") or "").strip().lower())
                if rec_url and clean_url and rec_url == clean_url:
                    return True, rec

        return False, None

    def record_job(
        self,
        company: str,
        role: str,
        source: str = "Naukri",
        job_url: str = "",
        match_score: float = 0.0,
        status: str = "Found",
        resume_version: str = "default",
        notes: str = "",
        job_id: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        filled_answers: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Records or updates a job application status.
        """
        if status not in self.ALLOWED_STATUSES:
            raise ValueError(f"Status '{status}' not in allowed statuses: {self.ALLOWED_STATUSES}")

        key = self._normalize_key(company, role, job_url, job_id)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        entry = self.records.get(key, {})
        entry.update({
            "key": key,
            "date": entry.get("date", now_str),
            "last_updated": now_str,
            "company": company.strip(),
            "role": role.strip(),
            "source": source.strip(),
            "job_url": job_url.strip(),
            "match_score": match_score,
            "status": status,
            "resume_version": resume_version,
            "notes": notes.strip(),
            "job_id": job_id or entry.get("job_id", ""),
            "filled_answers": filled_answers or entry.get("filled_answers", []),
            "metadata": extra_metadata or entry.get("metadata", {})
        })

        self.records[key] = entry
        self._save()
        return entry

    def update_status(self, key_or_url: str, new_status: str, notes: Optional[str] = None, filled_answers: Optional[List[Dict[str, str]]] = None):
        """
        Updates status, notes, and filled questionnaire answers for an existing record.
        """
        if new_status not in self.ALLOWED_STATUSES:
            raise ValueError(f"Status '{new_status}' not in allowed statuses")

        target_entry = None
        if key_or_url in self.records:
            target_entry = self.records[key_or_url]
        else:
            clean_target = re.sub(r'[?#].*$', '', (key_or_url or '').strip().lower())
            for rec in self.records.values():
                rec_url = re.sub(r'[?#].*$', '', (rec.get("job_url") or '').strip().lower())
                rec_id = str(rec.get("job_id") or '').strip().lower()
                if rec.get("job_url") == key_or_url or (clean_target and rec_url and clean_target == rec_url) or (rec_id and rec_id in key_or_url.lower()):
                    target_entry = rec
                    break

        if target_entry:
            target_entry["status"] = new_status
            target_entry["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if notes is not None:
                target_entry["notes"] = notes
            if filled_answers is not None:
                existing_answers = target_entry.get("filled_answers", [])
                # Merge new unique answers
                existing_qs = {a.get("question") for a in existing_answers}
                for a in filled_answers:
                    if a.get("question") not in existing_qs:
                        existing_answers.append(a)
                target_entry["filled_answers"] = existing_answers
            self._save()

    def record_unresolved_question(self, question_text: str, company: str = "", job_title: str = "", job_url: str = "", options: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Persists unmapped questions to data/unresolved_questions.json for user resolution.
        """
        unresolved_file = "data/unresolved_questions.json"
        data = {"unresolved": []}
        if os.path.exists(unresolved_file):
            try:
                with open(unresolved_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {"unresolved": []}

        unresolved_list = data.get("unresolved", [])
        clean_q = question_text.strip()

        # Check for duplicate question
        existing = next((item for item in unresolved_list if item.get("question", "").strip().lower() == clean_q.lower()), None)
        if existing:
            existing["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if company and company not in existing.get("company", ""):
                existing["company"] = f"{existing.get('company', '')}, {company}"
            entry = existing
        else:
            q_id = f"q_{len(unresolved_list) + 1}_{int(datetime.now().timestamp())}"
            entry = {
                "id": q_id,
                "question": clean_q,
                "company": company or "Hiring Company",
                "job_title": job_title or "Software Developer",
                "job_url": job_url or "",
                "first_seen": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "options": options or [],
                "status": "pending"
            }
            unresolved_list.insert(0, entry)

        data["unresolved"] = unresolved_list
        os.makedirs(os.path.dirname(unresolved_file), exist_ok=True)
        with open(unresolved_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return entry

    def get_review_queue(self) -> List[Dict[str, Any]]:
        """
        Returns all jobs currently waiting in 'Under Review' or 'Waiting for Input'.
        """
        return [
            rec for rec in self.records.values()
            if rec.get("status") in ("Under Review", "Waiting for Input")
        ]

    def get_stats(self) -> Dict[str, Any]:
        """
        Aggregates summary statistics for report generation.
        """
        stats = {
            "total_records": len(self.records),
            "submitted": 0,
            "under_review": 0,
            "waiting_for_input": 0,
            "skipped": 0,
            "already_applied": 0,
            "rejected": 0,
            "failed": 0,
            "closed": 0,
            "by_source": {}
        }
        for rec in self.records.values():
            st = rec.get("status", "")
            src = rec.get("source", "Naukri")
            stats["by_source"][src] = stats["by_source"].get(src, 0) + 1

            if st == "Submitted":
                stats["submitted"] += 1
            elif st == "Under Review":
                stats["under_review"] += 1
            elif st == "Waiting for Input":
                stats["waiting_for_input"] += 1
            elif st == "Skipped":
                stats["skipped"] += 1
            elif st == "Already Applied":
                stats["already_applied"] += 1
            elif st == "Rejected":
                stats["rejected"] += 1
            elif st == "Failed":
                stats["failed"] += 1
            elif st == "Closed":
                stats["closed"] += 1

        return stats

    def print_summary(self):
        """
        Prints structured summary to terminal.
        """
        stats = self.get_stats()
        print("\n" + "=" * 60)
        print("📊 NAUKRI APPLICATION TRACKER SUMMARY")
        print("=" * 60)
        print(f"Total Applications Tracked: {stats['total_records']}")
        print(f"  • Submitted:              {stats['submitted']}")
        print(f"  • Under Review:           {stats['under_review']}")
        print(f"  • Waiting for Input:      {stats['waiting_for_input']}")
        print(f"  • Skipped:                {stats['skipped']}")
        print(f"  • Already Applied:        {stats['already_applied']}")
        print(f"  • Failed:                 {stats['failed']}")
        print("\nBy Source:")
        for src, cnt in stats["by_source"].items():
            print(f"  • {src}: {cnt}")
        print("=" * 60 + "\n")

    def _export_markdown(self):
        """
        Renders the persistent APPLICATIONS_TRACKER.md file.
        """
        lines = [
            "# 📋 Naukri Job Application Tracker",
            "",
            "> Real-time persistent log of all discovered, reviewed, and submitted Naukri job applications.",
            "",
            "| Date | Company | Role | Source | Job URL | Match Score | Status | Resume Version | Notes |",
            "| ---- | ------- | ---- | ------ | ------- | ----------- | ------ | -------------- | ----- |"
        ]

        sorted_records = sorted(
            self.records.values(),
            key=lambda x: x.get("date", ""),
            reverse=True
        )

        for r in sorted_records:
            date = r.get("date", "")
            comp = r.get("company", "").replace("|", "-")
            role = r.get("role", "").replace("|", "-")
            src = r.get("source", "Naukri").replace("|", "-")
            url = r.get("job_url", "")
            url_md = f"[{comp} Role]({url})" if url else "N/A"
            score = f"{r.get('match_score', 0):.1f}"
            st = r.get("status", "")
            resume = r.get("resume_version", "default")
            notes = r.get("notes", "").replace("|", "-")

            lines.append(f"| {date} | {comp} | {role} | {src} | {url_md} | {score} | **{st}** | {resume} | {notes} |")

        lines.append("")
        with open(self.markdown_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
