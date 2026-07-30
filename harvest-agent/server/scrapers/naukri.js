import puppeteer from "puppeteer";

const parseAge = (text = "") => {
  const t = text.toLowerCase().trim();
  const now = Date.now();
  if (!t || t.includes("just") || t.includes("today")) return new Date(now);
  const h = t.match(/(\d+)\s*hour/);  if (h) return new Date(now - h[1] * 36e5);
  const d = t.match(/(\d+)\s*day/);   if (d) return new Date(now - d[1] * 864e5);
  const w = t.match(/(\d+)\s*week/);  if (w) return new Date(now - w[1] * 6048e5);
  const m = t.match(/(\d+)\s*month/); if (m) return new Date(now - m[1] * 2592e6);
  return new Date(now);
};

export async function scrapeNaukri({ keyword = "react developer", maxJobs = 20 } = {}) {
  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
  });

  try {
    const page = await browser.newPage();
    await page.setUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    );
    await page.setViewport({ width: 1280, height: 900 });

    const slug = keyword.replace(/\s+/g, "-").toLowerCase();
    console.log(`  [Naukri] Searching: ${keyword}`);
    await page.goto(`https://www.naukri.com/${slug}-jobs`, {
      waitUntil: "networkidle2",
      timeout: 30000,
    });

    await page.waitForSelector("article.jobTuple", { timeout: 15000 }).catch(() => {});

    const raw = await page.evaluate(() => {
      const cards = [...document.querySelectorAll("article.jobTuple")];
      return cards.map((c) => ({
        title:      c.querySelector("a.title")?.innerText?.trim()       || null,
        company:    c.querySelector("a.comp-name")?.innerText?.trim()   || null,
        poc:        c.querySelector(".rec-name a")?.innerText?.trim()   || null,
        postedRaw:  c.querySelector(".job-post-day")?.innerText?.trim() || null,
        linkedin:   c.querySelector(".rec-name a")?.href                || null,
      }));
    });

    return raw
      .filter((r) => r.title && r.company)
      .slice(0, maxJobs)
      .map((r) => ({
        title:      r.title,
        company:    r.company,
        source:     "Naukri",
        poc:        r.poc    || null,
        postedDate: parseAge(r.postedRaw),
        email:      null,
        whatsapp:   null,
        linkedin:   r.linkedin || null,
        mobile:     null,
      }));
  } finally {
    await browser.close();
  }
}
