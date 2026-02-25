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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")

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
# HELPERS
# ─────────────────────────────────────────────────────────────────
def extract_salary(text):
    m = re.search(
        r"\$[\d,]+(?:k)?(?:\s*[-–]\s*\$?[\d,]+(?:k)?)?(?:\s*(?:USD|/yr|/year|annually))?",
        text, re.IGNORECASE
    )
    return m.group(0).strip() if m else ""


def make_absolute(href, base_url):
    if not href:
        return ""
    if href.startswith("http"):
        return href
    domain = "/".join(base_url.split("/")[:3])  # https://example.com
    return domain + href


# ─────────────────────────────────────────────────────────────────
# RSS SCRAPERS — fast, reliable, no browser needed
# ─────────────────────────────────────────────────────────────────
def is_recent(entry, days=30):
    """Return True if the RSS entry was published within the last N days."""
    import time
    import email.utils

    # Try standard parsed date fields first
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if published:
        age_days = (time.time() - time.mktime(published)) / 86400
        return age_days <= days

    # Fallback: try parsing the raw published string manually
    raw = entry.get("published") or entry.get("updated") or ""
    if raw:
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
            age_days = (datetime.now(parsed.tzinfo) - parsed).days
            return age_days <= days
        except Exception:
            pass

    # WWR sometimes embeds the date in the summary text e.g. "December 7, 2023"
    summary = entry.get("summary", "")
    date_match = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2},?\s+(\d{4})", summary
    )
    if date_match:
        try:
            from datetime import datetime as dt
            parsed = dt.strptime(date_match.group(0).replace(",", ""), "%B %d %Y")
            age_days = (dt.now() - parsed).days
            return age_days <= days
        except Exception:
            pass

    # No date found — include it to avoid discarding valid jobs
    return True


def scrape_weworkremotely_rss():
    """
    WWR exposes category-specific RSS feeds. Using the back-end specific
    feed to avoid noise from sales, frontend, and management roles.
    """
    import feedparser
    from bs4 import BeautifulSoup
    source = "We Work Remotely"
    # Back-End Programming category is more targeted than general programming
    url = "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss"
    jobs = []
    print(f"  Scraping {source} (RSS)...")
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            if not is_recent(entry, days=30):
                continue

            title = entry.get("title", "").strip()
            link  = entry.get("link", "").strip()

            # WWR RSS title format is "Company: Job Title"
            company, _, job_title = title.partition(": ")
            if not job_title:
                job_title = title
                company = ""

            raw_summary = entry.get("summary", "")
            # Use full summary text — this is the actual job description from WWR's RSS
            # We truncate to 2000 chars which is plenty for Ollama scoring
            summary_full = BeautifulSoup(raw_summary, "html.parser").get_text()[:2000].strip()
            summary_short = summary_full[:300]
            salary = extract_salary(summary_full)

            if not job_title or not link:
                continue

            jobs.append({
                "title": job_title.strip(),
                "company": company.strip(),
                "link": link,
                "salary": salary,
                "summary": summary_short,
                # Pre-populate description from RSS so we don't need to visit the page
                "description": summary_full,
                "source": source,
            })
    except Exception as e:
        print(f"    {source} RSS failed: {e}")

    print(f"    Found {len(jobs)} listings (last 30 days)")
    return jobs


def scrape_remotive_rss():
    """
    Remotive's RSS feed is clean and includes title, company, and tags.
    """
    import feedparser
    from bs4 import BeautifulSoup
    source = "Remotive"
    url = "https://remotive.com/remote-jobs/feed/software-dev"
    jobs = []
    print(f"  Scraping {source} (RSS)...")
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            # Skip stale listings
            if not is_recent(entry, days=30):
                continue

            title   = entry.get("title", "").strip()
            link    = entry.get("link", "").strip()
            company = entry.get("author", "").strip()

            raw_summary = entry.get("summary", "")
            summary_full = BeautifulSoup(raw_summary, "html.parser").get_text()[:2000].strip()
            summary_short = summary_full[:300]
            salary = extract_salary(summary_full)

            if not title or not link:
                continue

            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "salary": salary,
                "summary": summary_short,
                "description": summary_full,
                "source": source,
            })
    except Exception as e:
        print(f"    {source} RSS failed: {e}")

    print(f"    Found {len(jobs)} listings (last 30 days)")
    return jobs


async def scrape_himalayas(page):
    """
    Himalayas is a React app. Jobs render as <a> tags with href /jobs/...
    containing structured child elements for title, company, and tags.
    """
    source = "Himalayas"
    url = "https://himalayas.app/jobs/engineering"
    jobs = []
    print(f"  Scraping {source}...")
    try:
        await page.goto(url, wait_until="networkidle", timeout=40000)
        await page.wait_for_timeout(3000)

        # Each job card is an <a> linking to /jobs/<company>/<role>
        items = await page.query_selector_all("a[href^='/jobs/'][href*='/']")
        seen = set()
        for item in items:
            try:
                href = await item.get_attribute("href") or ""
                # Filter out non-job links like /jobs (the listing page itself)
                parts = href.strip("/").split("/")
                if len(parts) < 2:
                    continue
                if href in seen:
                    continue
                seen.add(href)

                text_content = (await item.inner_text()).strip()
                lines = [l.strip() for l in text_content.split("\n") if l.strip()]
                if not lines:
                    continue

                title = lines[0]
                company = lines[1] if len(lines) > 1 else ""
                salary = extract_salary(text_content)

                jobs.append({
                    "title": title,
                    "company": company,
                    "link": make_absolute(href, url),
                    "salary": salary,
                    "summary": " | ".join(lines[2:5]),
                    "source": source,
                })
            except Exception:
                continue
    except Exception as e:
        print(f"    {source} failed: {e}")

    print(f"    Found {len(jobs)} listings")
    return jobs


async def scrape_workingnomads(page):
    """
    Working Nomads renders jobs as <li> elements with class 'job-item' or
    similar. Each has an <a class='job-title'> linking to the job detail.
    """
    source = "Working Nomads"
    url = "https://www.workingnomads.com/jobs?category=development"
    jobs = []
    print(f"  Scraping {source}...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        items = await page.query_selector_all(".job-item, li[class*='job']")
        for item in items:
            try:
                title_el = await item.query_selector("a.job-title, h2 a, h3 a")
                if not title_el:
                    continue
                title = (await title_el.inner_text()).strip()
                href = await title_el.get_attribute("href") or ""

                company_el = await item.query_selector(".company, [class*='company']")
                salary_el = await item.query_selector("[class*='salary']")

                company = (await company_el.inner_text()).strip() if company_el else ""
                salary = (await salary_el.inner_text()).strip() if salary_el else ""

                if not title:
                    continue

                jobs.append({
                    "title": title,
                    "company": company,
                    "link": make_absolute(href, url),
                    "salary": salary,
                    "summary": company,
                    "source": source,
                })
            except Exception:
                continue
    except Exception as e:
        print(f"    {source} failed: {e}")

    print(f"    Found {len(jobs)} listings")
    return jobs


async def scrape_all_boards(page):
    """
    Combine RSS scrapers (fast, reliable) with Playwright scrapers
    (for JS-heavy sites). Returns deduplicated job list.
    """
    all_jobs = []

    # RSS-based — no browser needed, fast and reliable
    all_jobs += scrape_weworkremotely_rss()
    all_jobs += scrape_remotive_rss()

    # Playwright-based — for sites without clean RSS feeds
    all_jobs += await scrape_himalayas(page)
    all_jobs += await scrape_workingnomads(page)

    # Deduplicate by link
    seen = set()
    unique = []
    for job in all_jobs:
        if job["link"] and job["link"] not in seen:
            seen.add(job["link"])
            unique.append(job)

    print(f"\nTotal unique listings: {len(unique)}")
    return unique


async def fetch_job_detail(page, job):
    """
    Visit the job detail page and extract the actual job description.
    Skips jobs that already have a description populated from RSS.
    """
    # RSS jobs already have description pre-populated — skip the browser visit
    if job.get("description") and len(job["description"]) > 200:
        return job

    if not job.get("link"):
        return job
    try:
        await page.goto(job["link"], wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)

        source = job.get("source", "")

        # Source-specific selectors — most reliable
        source_selectors = {
            "We Work Remotely": [
                # WWR wraps job body in a div with id listing or class listing
                "#job-listing",
                ".listing-container > div:first-child",
                "section.job",
                # Fall back to any div with substantial text that isn't the widget
                "div.job",
            ],
            "Remotive": [
                ".job-description",
                "[class*='JobDescription']",
                "article",
            ],
            "Himalayas": [
                "[class*='description']",
                "[class*='JobDescription']",
                "article",
            ],
            "Working Nomads": [
                ".job-description",
                "article",
                "main",
            ],
        }

        # Generic fallback selectors — ordered from most to least specific
        generic_selectors = [
            ".job-description",
            "#job-description",
            "[class*='job-description']",
            "[class*='jobDescription']",
            "article",
            "main",
        ]

        # Noise phrases that indicate we've hit a widget, not the job content
        noise_phrases = [
            "learn the skills employers are hiring for",
            "powered by learnisa",
            "related jobs",
            "sign in to apply",
            "create an account",
        ]

        selectors = source_selectors.get(source, []) + generic_selectors

        for selector in selectors:
            try:
                # For WWR, try each matching element until we find one without noise
                els = await page.query_selector_all(selector)
                for el in els:
                    text = (await el.inner_text()).strip()
                    if len(text) < 150:
                        continue
                    text_lower = text.lower()
                    if any(phrase in text_lower for phrase in noise_phrases):
                        continue
                    job["description"] = text[:3000]
                    return job
            except Exception:
                continue

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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # Step 1: Scrape listings from all boards
        print("\n[1/3] Scraping job boards...")
        list_page = await context.new_page()
        all_jobs = await scrape_all_boards(list_page)

        # Step 2: Fetch full descriptions (cap at 40 to keep runtime reasonable)
        print("\n[2/3] Fetching job details...")
        detail_page = await context.new_page()
        unique_jobs = all_jobs[:40]
        for i, job in enumerate(unique_jobs):
            print(f"  [{i+1}/{len(unique_jobs)}] {job['title'][:60]}")
            await fetch_job_detail(detail_page, job)

        await browser.close()

#    print("\n=== RAW SCRAPED JOBS ===")
#    for job in all_jobs[:10]:
#        print(json.dumps(job, indent=2))
#    print("=== END DEBUG ===")
#    exit(0)

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
