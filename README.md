# Job Agent MVP

Scrapes remote backend engineering jobs, scores each one against your resume
using Ollama locally, and emails you a ranked daily digest.

---

## Setup

### 1. Install Python dependencies

```bash
pip3 install -r requirements.txt
playwright install chromium
playwright install-deps chromium
```

### 2. Set up a Gmail App Password

You need an App Password — NOT your regular Gmail password.

1. Go to myaccount.google.com → Security
2. Enable 2-Step Verification
3. Go to Security → App Passwords
4. Name it "Job Agent" → copy the 16-character password

### 3. Set up your .env file

Copy the example and fill in your values:

```bash
cp .env.example .env
nano .env
```

Fill in your Gmail details:
```
EMAIL_SENDER=your_agent_gmail@gmail.com
EMAIL_PASSWORD=xxxx xxxx xxxx xxxx
EMAIL_RECIPIENT=your_personal_email@gmail.com
```

The `.env` file is gitignored and will never be committed to GitHub.
The `.env.example` file is safe to commit — it contains no real secrets.

### 4. Make sure Ollama is running

```bash
ollama serve &
ollama pull qwen2.5:14b  # if not already pulled
```

### 5. Run it manually to test

```bash
python3 job_agent.py
```

The script will print progress as it runs. Expect it to take 5-15 minutes
depending on how fast your VM is — it's visiting 40 job pages and running
Ollama on each one. Check your inbox when it finishes.

### 6. Schedule daily with cron

```bash
crontab -e
```

Add this line to run every morning at 8am:

```
0 8 * * * /usr/bin/python3 /path/to/job_agent.py >> /path/to/job_agent.log 2>&1
```

Replace `/path/to/` with the actual directory where you saved the script.

---

## How it works

1. **Scrape** — Playwright opens a headless Chromium browser and visits
   We Work Remotely, Remotive, Himalayas, and Working Nomads. It pulls
   the listing titles and links from each board.

2. **Fetch details** — For each of the first 40 unique listings, it visits
   the actual job page and grabs the full description text.

3. **Score** — Each job description is sent to Ollama (qwen2.5:14b) along
   with your resume. Ollama returns a fit score (1-10), whether the role
   is backend/remote, an estimated salary, a fit summary, the strongest
   match reason, and any concerns.

4. **Filter** — Jobs that aren't backend, aren't remote, or score below 5
   are dropped.

5. **Email** — The top 10 by fit score are formatted into a clean HTML
   email and sent to you.

---

## Troubleshooting

**Playwright error on install:**
```bash
sudo apt install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
  libxrandr2 libgbm1 libasound2
```

**Ollama not responding:**
Make sure it's running: `ollama serve &`

**Gmail auth error:**
Double check you're using the App Password (16 chars, no spaces needed),
not your regular Gmail password. 2FA must be enabled on the account.

**0 jobs passing filters:**
Run the script and watch the output — it prints what each job scores.
You may need to lower the fit_score threshold in main() from 5 to 4.

---

## What's next (Phase 2)

Once you're happy with the digest, the next step is application drafting:
- Pick a job from the digest
- Agent opens the application page
- Ollama drafts answers to each form field based on your resume
- You review a document before anything gets submitted
