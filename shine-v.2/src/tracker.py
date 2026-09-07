import os
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple


class ApplicationTracker:
    """
    Manages persistent application tracking and deduplication for Shine Automation.
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
        "Failed",
        "Not Accepting"
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
        if clean_url and any(domain in clean_url for domain in ["shine.com/jobs/", "shine.com", "naukri.com", "wellfound.com", "indeed.com"]):
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
        source: str = "Shine",
        job_url: str = "",
        match_score: float = 0.0,
        status: str = "Found",
        resume_version: str = "default",
        notes: str = "",
        job_id: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None
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
            "company": (company or "Unknown").strip(),
            "role": (role or "Unknown").strip(),
            "source": source.strip(),
            "job_url": (job_url or "").strip(),
            "match_score": match_score,
            "status": status,
            "resume_version": resume_version,
            "notes": (notes or "").strip(),
            "job_id": job_id or entry.get("job_id", ""),
            "metadata": extra_metadata or entry.get("metadata", {})
        })

        self.records[key] = entry
        self._save()
        return entry

    def update_status(self, key_or_url: str, new_status: str, notes: Optional[str] = None):
        """
        Updates status and notes for an existing record.
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
            if notes:
                target_entry["notes"] = f"{target_entry.get('notes', '')} | {notes}".strip(" |")
            self._save()

    def get_stats(self) -> Dict[str, Any]:
        """
        Calculates aggregate tracker statistics.
        """
        total = len(self.records)
        counts = {}
        for s in self.ALLOWED_STATUSES:
            counts[s] = 0

        for r in self.records.values():
            st = r.get("status", "Found")
            counts[st] = counts.get(st, 0) + 1

        scores = [r.get("match_score", 0) for r in self.records.values() if r.get("match_score")]
        avg_score = (sum(scores) / len(scores)) if scores else 0.0

        return {
            "total_evaluated": total,
            "status_counts": counts,
            "submitted": counts.get("Submitted", 0),
            "under_review": counts.get("Under Review", 0),
            "waiting_for_input": counts.get("Waiting for Input", 0),
            "skipped": counts.get("Skipped", 0),
            "average_match_score": round(avg_score, 1)
        }

    def print_summary(self):
        """
        Prints clean formatted terminal summary.
        """
        stats = self.get_stats()
        print("\n=======================================================")
        print("📊 SHINE APPLICATION TRACKER SUMMARY")
        print("=======================================================")
        print(f"Total Jobs Evaluated:      {stats['total_evaluated']}")
        print(f"✅ Successfully Submitted:  {stats['submitted']}")
        print(f"📝 Jobs Under Review:      {stats['under_review']}")
        print(f"⏳ Waiting for User Input: {stats['waiting_for_input']}")
        print(f"⏭️ Jobs Skipped:            {stats['skipped']}")
        print(f"🎯 Average Match Score:    {stats['average_match_score']}/100")
        print("-------------------------------------------------------")
        print("Status Breakdown:")
        for status, count in stats["status_counts"].items():
            if count > 0:
                print(f"  • {status: <20}: {count}")
        print("=======================================================\n")

    def _export_markdown(self):
        """
        Exports the database into a clean human-readable Markdown table.
        """
        lines = [
            "# 📋 Job Application Tracker (Shine.com Automation)",
            "",
            f"**Last Updated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
            f"**Total Tracked:** {len(self.records)}",
            "",
            "| Date | Company | Role | Score | Status | Source | Resume | Notes | Job Link |",
            "| :--- | :--- | :--- | :---: | :--- | :--- | :--- | :--- | :---: |"
        ]

        # Sort records newest first
        sorted_records = sorted(
            self.records.values(),
            key=lambda x: x.get("date", ""),
            reverse=True
        )

        for rec in sorted_records:
            date_str = rec.get("date", "-")
            company = (rec.get("company") or "Unknown").replace("|", "-")
            role = (rec.get("role") or "Unknown").replace("|", "-")
            score = rec.get("match_score", 0.0)
            status = rec.get("status", "Found")
            source = rec.get("source", "Shine")
            resume = rec.get("resume_version", "default")
            notes = (rec.get("notes") or "").replace("|", "-")
            url = rec.get("job_url", "")
            url_cell = f"[View Job]({url})" if url else "-"

            # Emoji status formatting
            status_map = {
                "Submitted": "✅ Submitted",
                "Under Review": "📝 Under Review",
                "Applying": "⚡ Applying",
                "Waiting for Input": "⏳ Waiting for Input",
                "Already Applied": "🔄 Already Applied",
                "Skipped": "⏭️ Skipped",
                "Closed": "🔒 Closed",
                "Rejected": "❌ Rejected",
                "Failed": "⚠️ Failed",
                "Not Accepting": "🚫 Not Accepting"
            }
            formatted_status = status_map.get(status, status)

            lines.append(
                f"| {date_str} | {company} | {role} | {score} | {formatted_status} | {source} | {resume} | {notes} | {url_cell} |"
            )

        lines.append("")
        with open(self.markdown_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
