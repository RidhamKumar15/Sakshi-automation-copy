import os
from datetime import datetime
from typing import Any, Dict, List


class RunReporter:
    """
    Generates structured end-of-run reports conforming to user specification:
    - Total jobs found
    - Jobs evaluated
    - Jobs skipped
    - Jobs waiting for review
    - Applications completed
    - Applications requiring manual action
    - Errors encountered
    - List of job URLs that need attention
    """

    def __init__(self, output_dir: str = "data"):
        self.output_dir = output_dir

    def generate_report(self, run_results: List[Dict[str, Any]]) -> str:
        """
        Aggregates results across all processed sources and formats the final report.
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        total_found = sum(r.get("jobs_found", 0) for r in run_results)
        total_evaluated = sum(r.get("jobs_evaluated", 0) for r in run_results)
        total_skipped = sum(r.get("jobs_skipped", 0) for r in run_results)
        total_under_review = sum(r.get("jobs_under_review", 0) for r in run_results)
        total_submitted = sum(r.get("applications_submitted", 0) for r in run_results)
        total_manual = sum(r.get("applications_manual_action", 0) for r in run_results)
        duplicates_ignored = sum(r.get("duplicates_ignored", 0) for r in run_results)

        all_errors = []
        all_attention_urls = []
        for r in run_results:
            all_errors.extend(r.get("errors", []))
            all_attention_urls.extend(r.get("attention_urls", []))

        # Deduplicate attention URLs
        all_attention_urls = list(dict.fromkeys(all_attention_urls))

        # Format Terminal String
        terminal_lines = [
            "\n" + "=" * 65,
            "📊 JOB APPLICATION AUTOMATION RUN REPORT",
            f"⏰ Generated: {now_str}",
            "=" * 65,
            f"🔍 Total Jobs Found:                 {total_found}",
            f"📋 Jobs Evaluated:                  {total_evaluated}",
            f"⏭️ Duplicates Ignored:               {duplicates_ignored}",
            f"❌ Jobs Skipped (<55 Score):         {total_skipped}",
            f"📝 Jobs Waiting for Review (55-69):  {total_under_review}",
            f"🎉 Applications Completed:           {total_submitted}",
            f"🚨 Applications Needing Action:      {total_manual}",
            f"⚠️ Errors Encountered:               {len(all_errors)}",
            "-" * 65
        ]

        if all_attention_urls:
            terminal_lines.append("\n🔗 Job URLs Requiring Your Attention:")
            for idx, url in enumerate(all_attention_urls, 1):
                terminal_lines.append(f"  {idx}. {url}")

        if all_errors:
            terminal_lines.append("\n⚠️ Errors Detail:")
            for idx, err in enumerate(all_errors, 1):
                terminal_lines.append(f"  {idx}. {err}")

        terminal_lines.append("=" * 65 + "\n")
        report_text = "\n".join(terminal_lines)

        # Save to disk as latest_run_report.md
        md_lines = [
            f"# 📊 Job Application Automation Run Report",
            f"**Timestamp:** `{now_str}`",
            "",
            "## Summary Metrics",
            f"- **Total Jobs Found:** `{total_found}`",
            f"- **Jobs Evaluated:** `{total_evaluated}`",
            f"- **Duplicates Ignored:** `{duplicates_ignored}`",
            f"- **Jobs Skipped (<55 Score):** `{total_skipped}`",
            f"- **Jobs Waiting for Review (55-69):** `{total_under_review}`",
            f"- **Applications Completed:** `{total_submitted}`",
            f"- **Applications Requiring Manual Action:** `{total_manual}`",
            f"- **Errors Encountered:** `{len(all_errors)}`",
            "",
            "## Action Items & Review Queue"
        ]

        if all_attention_urls:
            md_lines.append("The following job listings are waiting in your review queue or require manual input:")
            for idx, url in enumerate(all_attention_urls, 1):
                md_lines.append(f"{idx}. [{url}]({url})")
        else:
            md_lines.append("No immediate action items. All eligible jobs processed successfully.")

        if all_errors:
            md_lines.append("\n## Errors")
            for err in all_errors:
                md_lines.append(f"- {err}")

        os.makedirs(self.output_dir, exist_ok=True)
        report_path = os.path.join(self.output_dir, "latest_run_report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))

        return report_text
