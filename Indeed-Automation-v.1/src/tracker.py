import os
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple


class ApplicationTracker:
    """
    Manages persistent application tracking and deduplication for Indeed Automation.
    Maintains:
    - data/tracker.json (structured JSON storage)
    - APPLICATIONS_TRACKER.md (human-readable Markdown table)
    - data/unresolved_questions.json (screener questions needing user response)
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
        
        # Priority 1: Extract Indeed jk param if present in URL
        jk_match = re.search(r'[?&]jk=([a-zA-Z0-9]+)', (url or ""))
        if jk_match:
            return f"id:{jk_match.group(1).lower()}"

        norm_company = re.sub(r'[^a-z0-9]', '', (company or "").lower())
        norm_role = re.sub(r'[^a-z0-9]', '', (role or "").lower())
        if norm_company and norm_role:
            return f"comp_role:{norm_company}:{norm_role}"

        if url:
            # Strip tracking parameters only, preserve path & id
            clean_url = re.sub(r'[?&](utm_[^&]+|from=[^&]+|vjs=[^&]+|tk=[^&]+|advn=[^&]+|camk=[^&]+|xkcb=[^&]+|mobtk=[^&]+)', '', (url or "").strip().lower())
            clean_url = clean_url.rstrip('?&')
            return f"url:{clean_url}"

        return f"job:{norm_company or norm_role or 'unknown'}"

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

        # Check by Job Key (jk)
        target_jk = str(job_id).strip().lower() if (job_id and str(job_id).strip()) else None
        if not target_jk and url:
            m = re.search(r'[?&]jk=([a-zA-Z0-9]+)', url)
            if m:
                target_jk = m.group(1).lower()

        if target_jk:
            for rec in self.records.values():
                rec_id = str(rec.get("job_id") or "").strip().lower()
                rec_url = str(rec.get("job_url") or "")
                rec_jk = None
                if rec_id:
                    rec_jk = rec_id
                else:
                    m = re.search(r'[?&]jk=([a-zA-Z0-9]+)', rec_url)
                    if m:
                        rec_jk = m.group(1).lower()
                if rec_jk and rec_jk == target_jk:
                    return True, rec

        # Check by Company + Role combination
        norm_comp = re.sub(r'[^a-z0-9]', '', (company or "").lower())
        norm_r = re.sub(r'[^a-z0-9]', '', (role or "").lower())
        if norm_comp and norm_r and len(norm_comp) >= 3 and len(norm_r) >= 4:
            for rec in self.records.values():
                r_comp = re.sub(r'[^a-z0-9]', '', (rec.get("company") or "").lower())
                r_role = re.sub(r'[^a-z0-9]', '', (rec.get("role") or "").lower())
                if r_comp and r_role and r_comp == norm_comp and r_role == norm_r:
                    return True, rec

        return False, None

    def record_job(
        self,
        company: str,
        role: str,
        source: str = "Indeed",
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

    def update_status(
        self,
        key_or_url: str,
        new_status: str,
        notes: Optional[str] = None,
        filled_answers: Optional[List[Dict[str, str]]] = None,
        job_id: Optional[str] = None,
        company: Optional[str] = None,
        role: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Updates status, notes, and filled questionnaire answers for an existing record.
        """
        if new_status not in self.ALLOWED_STATUSES:
            raise ValueError(f"Status '{new_status}' not in allowed statuses: {self.ALLOWED_STATUSES}")

        target_entry = None

        # 1. Direct key match
        if key_or_url in self.records:
            target_entry = self.records[key_or_url]

        # 2. Check by job_id parameter or id: prefix
        if not target_entry and job_id:
            jk_key = f"id:{str(job_id).strip().lower()}"
            if jk_key in self.records:
                target_entry = self.records[jk_key]

        # 3. Check if key_or_url is a raw job_id
        if not target_entry and key_or_url:
            raw_id_key = f"id:{key_or_url.strip().lower()}"
            if raw_id_key in self.records:
                target_entry = self.records[raw_id_key]

        # 4. Extract jk query parameter from URL
        if not target_entry and key_or_url:
            jk_match = re.search(r'[?&]jk=([a-zA-Z0-9]+)', key_or_url)
            if jk_match:
                jk_key = f"id:{jk_match.group(1).lower()}"
                if jk_key in self.records:
                    target_entry = self.records[jk_key]

        # 5. Check normalized key using company + role if provided
        if not target_entry and company and role:
            cr_key = self._normalize_key(company, role, key_or_url, job_id)
            if cr_key in self.records:
                target_entry = self.records[cr_key]

        # 6. Fallback scan by exact job_url or matching job_id
        if not target_entry:
            target_jk = None
            if job_id:
                target_jk = str(job_id).strip().lower()
            elif key_or_url:
                m = re.search(r'[?&]jk=([a-zA-Z0-9]+)', key_or_url)
                if m:
                    target_jk = m.group(1).lower()

            for rec in self.records.values():
                # Exact URL match
                if rec.get("job_url") and key_or_url and rec.get("job_url").strip() == key_or_url.strip():
                    target_entry = rec
                    break
                # Match by extracted or stored job_id
                rec_id = str(rec.get("job_id") or "").strip().lower()
                if target_jk and rec_id and rec_id == target_jk:
                    target_entry = rec
                    break
                # Match jk parameter inside stored job_url
                if target_jk and rec.get("job_url"):
                    rm = re.search(r'[?&]jk=([a-zA-Z0-9]+)', rec.get("job_url"))
                    if rm and rm.group(1).lower() == target_jk:
                        target_entry = rec
                        break

        if target_entry:
            target_entry["status"] = new_status
            target_entry["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if notes is not None:
                target_entry["notes"] = notes
            if filled_answers is not None:
                existing_answers = target_entry.get("filled_answers", [])
                existing_qs = {a.get("question") for a in existing_answers}
                for a in filled_answers:
                    if a.get("question") not in existing_qs:
                        existing_answers.append(a)
                target_entry["filled_answers"] = existing_answers
            self._save()
            return target_entry
        else:
            print(f"[Tracker] ⚠️ Warning: update_status could not find record for '{key_or_url}'")
            return None

    def cleanup_stale_applying_records(self, default_status: str = "Failed", default_note: str = "Application interrupted before completion") -> int:
        """
        Scans records for any lingering 'Applying' status left behind by interrupted/terminated sessions
        and transitions them to an actionable status (default: 'Failed').
        """
        updated_count = 0
        for rec in self.records.values():
            if rec.get("status") == "Applying":
                rec["status"] = default_status
                rec["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                if not rec.get("notes") or "Applying now" in rec.get("notes", ""):
                    rec["notes"] = default_note
                updated_count += 1
        if updated_count > 0:
            self._save()
            print(f"[Tracker] Cleaned up {updated_count} stale 'Applying' record(s) -> '{default_status}'.")
        return updated_count

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
            src = rec.get("source", "Indeed")
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
        print("📊 INDEED APPLICATION TRACKER SUMMARY")
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
            "# 📋 Indeed Job Application Tracker",
            "",
            "> Real-time persistent log of all discovered, reviewed, and submitted Indeed job applications.",
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
            src = r.get("source", "Indeed").replace("|", "-")
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
