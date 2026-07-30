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

export async function scrapeDice({ keyword = "react developer", maxJobs = 20 } = {}) {
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

    console.log(`  [Dice] Searching: ${keyword}`);
    const url = `https://www.dice.com/jobs?q=${encodeURIComponent(keyword)}&pageSize=${maxJobs}`;
    await page.goto(url, { waitUntil: "networkidle2", timeout: 30000 });

    await page.waitForSelector("dhi-search-result", { timeout: 15000 }).catch(() => {
      // fallback: wait for any job card container
      return page.waitForSelector(".search-result-item", { timeout: 5000 }).catch(() => {});
    });

    // Small delay for JS rendering
    await new Promise((r) => setTimeout(r, 2000));

    const raw = await page.evaluate(() => {
      // Dice renders custom web components — gather all visible job cards
      const cards = [
        ...document.querySelectorAll("dhi-search-result"),
        ...document.querySelectorAll(".search-result-item"),
      ];

      return cards.map((c) => {
        const titleEl   = c.querySelector("[data-cy='card-title-link']") || c.querySelector("a.title-link");
        const compEl    = c.querySelector("[data-cy='search-result-company-name']") || c.querySelector(".company-name");
        const postedEl  = c.querySelector("[data-cy='card-posted-date']") || c.querySelector(".posted-date");
        const emailEl   = c.querySelector("a[href^='mailto:']");
        const linkedinEl = c.querySelector("a[href*='linkedin.com']");

        return {
          title:     titleEl?.innerText?.trim()   || null,
          company:   compEl?.innerText?.trim()    || null,
          postedRaw: postedEl?.innerText?.trim()  || null,
          email:     emailEl ? emailEl.href.replace("mailto:", "") : null,
          linkedin:  linkedinEl?.href             || null,
        };
      });
    });

    return raw
      .filter((r) => r.title && r.company)
      .slice(0, maxJobs)
      .map((r) => ({
        title:      r.title,
        company:    r.company,
        source:     "Dice",
        poc:        null,
        postedDate: parseAge(r.postedRaw),
        email:      r.email    || null,
        whatsapp:   null,
        linkedin:   r.linkedin || null,
        mobile:     null,
      }));
  } finally {
    await browser.close();
  }
}
