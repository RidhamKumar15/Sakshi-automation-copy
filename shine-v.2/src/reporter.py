import os
from datetime import datetime
from typing import Dict, Any, List


class RunReporter:
    """
    Generates structured end-of-run application reports in terminal and Markdown.
    """

    def __init__(self, output_file: str = "data/latest_run_report.md"):
        self.output_file = output_file

    def generate_report(self, results: List[Dict[str, Any]]) -> str:
        """
        Builds a comprehensive report from portal run results.
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_found = sum(r.get("jobs_found", 0) for r in results)
        total_eval = sum(r.get("jobs_evaluated", 0) for r in results)
        total_sub = sum(r.get("applications_submitted", 0) for r in results)
        total_rev = sum(r.get("jobs_under_review", 0) for r in results)
        total_skip = sum(r.get("jobs_skipped", 0) for r in results)
        total_dup = sum(r.get("duplicates_ignored", 0) for r in results)
        total_man = sum(r.get("applications_manual_action", 0) for r in results)

        all_errors = []
        all_attention_urls = []
        for r in results:
            all_errors.extend(r.get("errors", []))
            all_attention_urls.extend(r.get("attention_urls", []))

        lines = [
            "# 📊 Shine Job Application Automation - Run Summary",
            f"**Execution Timestamp:** {now_str}",
            "",
            "## 📈 Key Metrics",
            f"- **Total Job Postings Discovered:** {total_found}",
            f"- **Total Postings Evaluated:** {total_eval}",
            f"- **✅ Successfully Submitted Applications:** {total_sub}",
            f"- **📝 Jobs Placed in Review Queue:** {total_rev}",
            f"- **⏭️ Jobs Skipped (< Threshold):** {total_skip}",
            f"- **🔄 Duplicate Postings Filtered:** {total_dup}",
            f"- **⚠️ Jobs Requiring Human Attention:** {total_man}",
            "",
            "## 🌐 Source Breakdown"
        ]

        for r in results:
            src = r.get("source", "Portal")
            lines.append(f"### {src}")
            lines.append(f"- **Evaluated:** {r.get('jobs_evaluated', 0)}")
            lines.append(f"- **Submitted:** {r.get('applications_submitted', 0)}")
            lines.append(f"- **Under Review:** {r.get('jobs_under_review', 0)}")
            lines.append(f"- **Skipped:** {r.get('jobs_skipped', 0)}")
            lines.append(f"- **Duplicates Ignored:** {r.get('duplicates_ignored', 0)}")
            lines.append("")

        if all_attention_urls:
            lines.append("## 🚨 Action Required / Needs Human Input")
            for url in set(all_attention_urls):
                lines.append(f"- {url}")
            lines.append("")

        if all_errors:
            lines.append("## ❌ Errors Encountered")
            for err in set(all_errors):
                lines.append(f"- `{err}`")
            lines.append("")

        lines.append("---")
        lines.append("Review detailed statuses and full logs in [`APPLICATIONS_TRACKER.md`](APPLICATIONS_TRACKER.md).")
        lines.append("")

        md_content = "\n".join(lines)

        os.makedirs(os.path.dirname(self.output_file), exist_ok=True)
        with open(self.output_file, "w", encoding="utf-8") as f:
            f.write(md_content)

        return md_content
