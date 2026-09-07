# AGENT INSTRUCTIONS & WORKSPACE RULES

## 1. MANDATORY SKILL USAGE & ADHERENCE
- **Always Check and Follow Installed Skills**: Before executing any task, inspect the installed skills located in `.agents/skills/` (and any global agent skills available). 
- **Strict Skill Selection**:
  - For browser automation, web navigation, form filling, and UI interaction: ALWAYS use and follow the instructions in [`agent-browser`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/.agents/skills/agent-browser/SKILL.md) or [`browser-act`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/.agents/skills/browser-act/SKILL.md).
  - For web scraping, content extraction, search, crawling, and parsing: ALWAYS use the dedicated Firecrawl skills (`firecrawl-scrape`, `firecrawl-search`, `firecrawl-crawl`, `firecrawl-agent`, `firecrawl-parse`, `firecrawl-interact`, `firecrawl-download`, `firecrawl-map`, `firecrawl-monitor`, `firecrawl-research-index`, `firecrawl-developer-index`) or [`just-scrape`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/.agents/skills/just-scrape/SKILL.md).
  - For testing and QA: ALWAYS follow [`playwright-best-practices`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/.agents/skills/playwright-best-practices/SKILL.md) and [`webapp-testing`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/.agents/skills/webapp-testing/SKILL.md).
- **No Bypassing of Skills**: Never resort to naive or generic workarounds when a specialized skill exists for the workflow. Read the skill's `SKILL.md` before executing unfamiliar actions.

---

## 2. STRICT COMPLIANCE WITH PROJECT DOCUMENTATION (`*.md` FILES)
- **Always Check Existing & Future Markdown Files**: You must proactively inspect, read, and strictly follow all markdown documentation present in the workspace, including any `.md` files added in the future.
- **Design System Compliance ([`design.md`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/design.md))**:
  - Whenever building, editing, or styling UI components, pages, or layouts, strictly adhere to the **Genesis** design specification in [`design.md`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/design.md).
  - **Colors**:
    - Primary: `#6366F1` (Indigo) / Primary Hover: `#4F46E5`
    - Secondary: `#20970B` (Reserved exclusively for DESIGN.md brand highlight)
    - Neutral: `#9C9C9C`, Background: `#FAFAFA`, Surface: `#FFFFFF`
    - Text Primary: `#0A0A0A`, Text Secondary: `#6B6B6B`, Border: `#E8E8EC`
    - Semantic: Success `#10B981`, Warning `#F59E0B`, Error `#EF4444`
  - **Typography**: Display Font: *General Sans* (Fontshare), Body Font: *DM Sans* (Google Fonts), Code Font: *JetBrains Mono* (Google Fonts).
  - **Elevation & Shadows**: Minimal flat styling with 1px border. Reserve shadow/glow for hover and focus states only.
  - **Spacing & Radius**: Strict 4px base grid; 6px radius for buttons/inputs/selects, 12px for kit cards/search, 9999px for badges/avatars/chips.
  - **Do's and Don'ts**: Strictly respect all constraints defined in [`design.md`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/design.md).

---

## 3. DEVELOPMENT & EXECUTION WORKFLOW
1. **Understand & Inspect**: Check repository structure, relevant skills, and markdown specification files prior to making modifications.
2. **Quality & Precision**: Ensure all generated code is clean, robust, well-structured, and strictly adheres to project conventions.
3. **Verification**: Always verify changes against the guidelines in skills and project docs.
