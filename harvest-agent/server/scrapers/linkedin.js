import puppeteer from "puppeteer";

export async function scrapeLinkedIn({ keyword = "react developer", maxJobs = 20 } = {}) {
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

    console.log(`  [LinkedIn] Searching: ${keyword}`);
    // f_JT=C filters for Contract jobs
    const url = `https://www.linkedin.com/jobs/search/?keywords=${encodeURIComponent(keyword)}&f_JT=C&sortBy=DD`;
    await page.goto(url, { waitUntil: "networkidle2", timeout: 30000 });

    await page.waitForSelector(".base-card", { timeout: 15000 }).catch(() => {});
    await new Promise((r) => setTimeout(r, 2000));

    const raw = await page.evaluate(() => {
      const cards = [...document.querySelectorAll("ul.jobs-search__results-list > li")];
      return cards.map((c) => {
        const timeEl = c.querySelector("time");
        return {
          title:      c.querySelector(".base-search-card__title")?.innerText?.trim()    || null,
          company:    c.querySelector(".base-search-card__subtitle a")?.innerText?.trim() ||
                      c.querySelector(".base-search-card__subtitle")?.innerText?.trim() || null,
          postedDate: timeEl?.getAttribute("datetime") || null,
          linkedin:   c.querySelector("a.base-card__full-link")?.href || null,
          poc:        c.querySelector(".job-search-card__person-name")?.innerText?.trim() || null,
          pocLinkedin:c.querySelector(".job-search-card__person-link")?.href || null,
        };
      });
    });

    return raw
      .filter((r) => r.title && r.company)
      .slice(0, maxJobs)
      .map((r) => ({
        title:      r.title,
        company:    r.company,
        source:     "LinkedIn",
        poc:        r.poc || null,
        postedDate: r.postedDate ? new Date(r.postedDate) : new Date(),
        email:      null,
        whatsapp:   null,
        linkedin:   r.pocLinkedin || r.linkedin || null,
        mobile:     null,
      }));
  } finally {
    await browser.close();
  }
}
