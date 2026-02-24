#!/usr/bin/env python3
"""
Job Agent MVP
-------------
1. Uses Playwright to browse remote job boards headlessly
2. Uses Ollama (qwen2.5:14b) to score each posting against Jonathan's resume
3. Emails a daily digest of the top 10 best-fit roles

Secrets are loaded from a .env file — never hardcoded.
See .env.example for the required variables.
"""

import asyncio
import json
import smtplib
import re
import os
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.async_api import async_playwright
import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────
# CONFIG — loaded from .env file, never hardcoded
# ─────────────────────────────────────────────────────────────────
load_dotenv()

def require_env(key):
    """Load an env var or exit with a helpful error if it's missing."""
    val = os.getenv(key)
    if not val:
        print(f"✗ Missing required environment variable: {key}")
        print(f"  Add it to your .env file. See .env.example for reference.")
        sys.exit(1)
    return val

EMAIL_SENDER    = require_env("EMAIL_SENDER")
EMAIL_PASSWORD  = require_env("EMAIL_PASSWORD")
EMAIL_RECIPIENT = require_env("EMAIL_RECIPIENT")

OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

SALARY_MIN = int(os.getenv("SALARY_MIN", "80000"))
SALARY_MAX = int(os.getenv("SALARY_MAX", "130000"))

# ─────────────────────────────────────────────────────────────────
# RESUME CONTEXT — used by Ollama to evaluate fit
# ─────────────────────────────────────────────────────────────────
RESUME = """
Name: Jonathan Westerfield
Level: SDE II (Mid-level, 5+ years)
Education: B.S. Computer Engineering, Texas A&M, 2019
Target: Remote backend software engineering, $80k-$130k

--- EXPERIENCE ---

Annapurna Labs (Amazon) | SDE II | Dec 2022 – Jul 2025
Machine Learning Acceleration Systems Software
- Built CoAP-based web infrastructure on ML accelerator devices; 100+ API endpoints
- Led fleet-wide GPU failure investigation; 60% improvement in manufacturing yields
- Cross-compatible libraries for PCIe, HBM, BMC/SMC server components
- Reduced Trainium unsellable rate from 53% to 13%
- Fleet health for Trn2, Trn1, Inf2 EC2 instances
- Fleet-wide logging via AWS CloudWatch

Amazon Advertising | SDE I | May 2021 – Dec 2022
Amazon Ad Exchange (AAX)
- Owned O&O ad space; enabled video ads via VAST + Amazon DSP
- Designed multi-bid auction with deduplication; $74MM annualized revenue impact
- Built Pricing Experimentation A/B testing framework
- Authored 13+ technical guides for developers and partners

Amazon AWS | SDE I | Jun 2020 – May 2021
AWS Directory Service
- Remediated critical security vulnerability across 7+ code packages
- Migrated canary monitoring Python 2 → Python 3

--- SKILLS ---
Languages: Python, Java, Lua
Tools: Git, Linux, VS Code, JetBrains
Cloud: AWS (EC2, CloudWatch, and more)
Other: Agile/Scrum, API design, distributed systems, ML infrastructure, ad tech
"""

# ─────────────────────────────────────────────────────────────────
# JOB BOARDS TO SCRAPE
# ─────────────────────────────────────────────────────────────────
JOB_BOARDS = [
    {
        "name": "Indeed",
        "url": "https://www.indeed.com/rss?q=backend+engineer&l=remote&sort=date",
    },
    {
        "name": "We Work Remotely",
        "url": "https://weworkremotely.com/categories/remote-programming-jobs",
    },
    {
        "name": "Remotive",
        "url": "https://remotive.com/remote-jobs/software-dev",
    },
    {
        "name": "Himalayas",
        "url": "https://himalayas.app/jobs/engineering",
    },
    {
        "name": "Working Nomads",
        "url": "https://www.workingnomads.com/jobs?category=development",
    },
]

# ─────────────────────────────────────────────────────────────────
# SCRAPER — extracts job listings from each board
# ─────────────────────────────────────────────────────────────────
async def scrape_board(page, board):
    """Generic scraper — pulls title, company, link, and any visible salary."""
    jobs = []
    print(f"  Scraping {board['name']}...")
    try:
        await page.goto(board["url"], wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)  # let JS render

        # Grab all anchor tags and visible text blocks
        # Strategy: find elements that look like job cards
        cards = await page.query_selector_all(
            "li, article, [class*='job'], [class*='Job'], [class*='listing'], [class*='Listing'], [class*='card'], [class*='Card']"
        )

        seen_titles = set()
        for card in cards[:60]:
            try:
                text = await card.inner_text()
                text = text.strip()
                if len(text) < 10 or len(text) > 2000:
                    continue

                # Find the first link inside the card
                link_el = await card.query_selector("a")
                link = ""
                if link_el:
                    link = await link_el.get_attribute("href") or ""
                    if link and not link.startswith("http"):
                        # Make relative URLs absolute
                        base = board["url"].split("/")[0] + "//" + board["url"].split("/")[2]
                        link = base + link

                # Use first line as title heuristic
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                if not lines:
                    continue
                title = lines[0][:120]

                # Deduplicate
                if title in seen_titles or len(title) < 5:
                    continue
                seen_titles.add(title)

                # Extract salary if visible in text
                salary = ""
                salary_match = re.search(
                    r"\$[\d,]+(?:k)?(?:\s*[-–]\s*\$?[\d,]+(?:k)?)?(?:\s*(?:USD|/yr|/year|annually))?",
                    text, re.IGNORECASE
                )
                if salary_match:
                    salary = salary_match.group(0).strip()

                jobs.append({
                    "title": title,
                    "link": link,
                    "salary": salary,
                    "summary": " | ".join(lines[1:6]),  # next few lines as summary
                    "source": board["name"],
                })
            except Exception:
                continue

    except Exception as e:
        print(f"    Failed: {e}")

    print(f"    Found {len(jobs)} listings")
    return jobs


async def fetch_job_detail(page, job):
    """Visit the job's detail page and extract the full description."""
    if not job.get("link"):
        return job
    try:
        await page.goto(job["link"], wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)
        # Pull main content — try common content selectors first
        for selector in ["[class*='description']", "[class*='content']", "article", "main", "body"]:
            el = await page.query_selector(selector)
            if el:
                text = await el.inner_text()
                text = text.strip()
                if len(text) > 200:
                    job["description"] = text[:3000]
                    break
    except Exception:
        pass
    return job


# ─────────────────────────────────────────────────────────────────
# OLLAMA — resume fit scoring
# ─────────────────────────────────────────────────────────────────
def score_job_with_ollama(job):
    """Ask Ollama to score a job posting against the resume. Returns fit score + notes."""
    description = job.get("description") or job.get("summary") or job["title"]

    prompt = f"""You are a career advisor evaluating a job posting for a software engineer.

CANDIDATE RESUME:
{RESUME}

JOB POSTING:
Title: {job['title']}
Source: {job['source']}
Salary listed: {job['salary'] or 'Not listed'}
Description:
{description[:2000]}

Evaluate this job for the candidate. Respond ONLY with a valid JSON object, no other text:
{{
  "fit_score": <integer 1-10, where 10 is perfect fit>,
  "is_backend": <true or false — is this a backend/systems engineering role?>,
  "is_remote": <true or false — is this explicitly remote?>,
  "estimated_salary": "<your best estimate of salary range if not listed, or the listed range>",
  "salary_in_range": <true if estimated salary overlaps $80k-$130k, false if clearly above or below>,
  "fit_summary": "<2 sentences: why this is or isn't a good fit for the candidate>",
  "key_match": "<the single strongest reason this matches their background>",
  "concern": "<the single biggest gap or concern, or 'None' if no concerns>"
}}"""

    try:
        response = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1}
        }, timeout=300)

        raw = response.json()["response"].strip()

        # Strip markdown fences if present
        if "```" in raw:
            raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`")

        result = json.loads(raw)
        return result

    except Exception as e:
        print(f"    Ollama scoring failed for '{job['title']}': {e}")
        return None


# ─────────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────────
def build_email(top_jobs):
    date_str = datetime.now().strftime("%B %d, %Y")

    def score_color(score):
        if score >= 8: return "#27ae60"
        if score >= 6: return "#f39c12"
        return "#e74c3c"

    def score_label(score):
        if score >= 8: return "Strong fit"
        if score >= 6: return "Decent fit"
        return "Weak fit"

    cards = ""
    for i, (job, analysis) in enumerate(top_jobs, 1):
        score = analysis.get("fit_score", 0)
        color = score_color(score)
        label = score_label(score)
        salary_display = analysis.get("estimated_salary") or job.get("salary") or "Not listed"
        concern = analysis.get("concern", "")
        concern_html = f'<div style="margin-top:8px;font-size:12px;color:#e74c3c;">⚠ {concern}</div>' \
                       if concern and concern.lower() != "none" else ""

        cards += f"""
        <div style="background:#fff;border-radius:8px;padding:20px;margin-bottom:16px;border-left:4px solid {color};box-shadow:0 1px 4px rgba(0,0,0,0.08);">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">
            <div>
              <div style="font-size:11px;color:#888;margin-bottom:4px;">#{i} &bull; {job['source']}</div>
              <div style="font-size:18px;font-weight:700;margin-bottom:4px;">
                <a href="{job['link']}" style="color:#1a73e8;text-decoration:none;">{job['title']}</a>
              </div>
              <div style="font-size:13px;color:#555;">💰 {salary_display}</div>
            </div>
            <div style="text-align:center;background:{color};color:#fff;border-radius:8px;padding:8px 14px;min-width:60px;">
              <div style="font-size:22px;font-weight:700;">{score}/10</div>
              <div style="font-size:11px;">{label}</div>
            </div>
          </div>
          <div style="margin-top:12px;font-size:13px;color:#444;line-height:1.6;">
            {analysis.get('fit_summary', '')}
          </div>
          <div style="margin-top:10px;font-size:12px;color:#27ae60;">✓ {analysis.get('key_match', '')}</div>
          {concern_html}
          <div style="margin-top:12px;">
            <a href="{job['link']}" style="display:inline-block;background:#1a73e8;color:#fff;padding:8px 16px;border-radius:6px;font-size:13px;text-decoration:none;font-weight:600;">View Job →</a>
          </div>
        </div>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;background:#f0f2f5;padding:24px;margin:0;">
      <div style="max-width:680px;margin:auto;">
        <div style="background:#1a73e8;border-radius:10px 10px 0 0;padding:28px;color:#fff;">
          <h1 style="margin:0 0 6px;font-size:24px;">🧑‍💻 Daily Job Digest</h1>
          <p style="margin:0;opacity:0.85;">{date_str} &bull; Top {len(top_jobs)} Remote Backend Roles Matched to Your Resume</p>
        </div>
        <div style="background:#f0f2f5;padding:20px 0;">
          {cards}
        </div>
        <div style="text-align:center;font-size:11px;color:#aaa;padding:12px;">
          Powered by Ollama + {OLLAMA_MODEL} &bull; Running on your own server
        </div>
      </div>
    </body></html>"""
    return html


def send_email(top_jobs):
    if not top_jobs:
        print("No jobs to send.")
        return

    html = build_email(top_jobs)
    date_str = datetime.now().strftime("%b %d, %Y")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🧑‍💻 Job Digest {date_str} — {len(top_jobs)} Matches Found"
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html"))

    try:
        print(f"Sending email to {EMAIL_RECIPIENT}...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        print("✓ Email sent!")
    except Exception as e:
        print(f"✗ Email failed: {e}")


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
async def main():
    print("=" * 55)
    print("Job Agent Starting —", datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("=" * 55)

    all_jobs = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # Step 1: Scrape listings from all boards
        print("\n[1/3] Scraping job boards...")
        list_page = await context.new_page()
        for board in JOB_BOARDS:
            jobs = await scrape_board(list_page, board)
            all_jobs.extend(jobs)

        print(f"\nTotal raw listings: {len(all_jobs)}")

        # Step 2: Fetch full descriptions for first 40 unique jobs
        # (cap to avoid running all night)
        print("\n[2/3] Fetching job details...")
        detail_page = await context.new_page()
        seen_links = set()
        unique_jobs = []
        for job in all_jobs:
            if job["link"] and job["link"] not in seen_links:
                seen_links.add(job["link"])
                unique_jobs.append(job)

        unique_jobs = unique_jobs[:40]  # cap at 40 detail fetches
        for i, job in enumerate(unique_jobs):
            print(f"  [{i+1}/{len(unique_jobs)}] {job['title'][:60]}")
            await fetch_job_detail(detail_page, job)

        await browser.close()

    # Step 3: Score each job with Ollama
    print("\n[3/3] Scoring jobs against your resume with Ollama...")
    scored = []
    for i, job in enumerate(unique_jobs):
        print(f"  [{i+1}/{len(unique_jobs)}] Scoring: {job['title'][:60]}")
        analysis = score_job_with_ollama(job)
        if not analysis:
            continue

        # Filter: must be backend, remote, and fit score >= 5
        if not analysis.get("is_backend"):
            continue
        if not analysis.get("is_remote"):
            continue
        if analysis.get("fit_score", 0) < 5:
            continue

        scored.append((job, analysis))

    # Sort by fit score descending
    scored.sort(key=lambda x: x[1].get("fit_score", 0), reverse=True)
    top_10 = scored[:10]

    print(f"\n✓ {len(scored)} jobs passed filters. Sending top {len(top_10)}.")

    # Step 4: Email the digest
    send_email(top_10)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
