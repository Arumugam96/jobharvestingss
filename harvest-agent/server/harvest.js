import "dotenv/config";
import mongoose from "mongoose";
import Job from "./models/Job.js";
import { scrapeNaukri }   from "./scrapers/naukri.js";
import { scrapeDice }     from "./scrapers/dice.js";
import { scrapeLinkedIn } from "./scrapers/linkedin.js";

const MONGO_URI  = process.env.MONGO_URI  || "mongodb://localhost:27017/harvest_agent";
const MAX_JOBS   = parseInt(process.env.MAX_JOBS_PER_SOURCE || "20", 10);
const KEYWORDS   = (process.env.KEYWORDS || "react developer,java developer,devops engineer")
  .split(",").map((k) => k.trim()).filter(Boolean);

// ── Helpers ───────────────────────────────────────────────────────────────────

function dedup(jobs) {
  const seen = new Set();
  return jobs.filter((j) => {
    const key = `${j.title?.toLowerCase()}|${j.company?.toLowerCase()}|${j.source}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

async function runScraper(name, fn, keyword) {
  try {
    const results = await fn({ keyword, maxJobs: MAX_JOBS });
    console.log(`  ✓ ${name} → ${results.length} jobs found`);
    return results;
  } catch (err) {
    console.error(`  ✗ ${name} failed: ${err.message}`);
    return [];
  }
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function harvest() {
  console.log("=".repeat(55));
  console.log("  HarvestAgent — Real-time Job Scraper");
  console.log("=".repeat(55));
  console.log(`Keywords : ${KEYWORDS.join(", ")}`);
  console.log(`Max/src  : ${MAX_JOBS}`);
  console.log(`MongoDB  : ${MONGO_URI}\n`);

  await mongoose.connect(MONGO_URI);
  console.log("Connected to MongoDB.\n");

  let totalInserted = 0;
  let totalUpdated  = 0;

  for (const keyword of KEYWORDS) {
    console.log(`\n── Keyword: "${keyword}" ──`);

    // Run all three scrapers in parallel for this keyword
    const [naukriJobs, diceJobs, linkedInJobs] = await Promise.all([
      runScraper("Naukri",   scrapeNaukri,   keyword),
      runScraper("Dice",     scrapeDice,     keyword),
      runScraper("LinkedIn", scrapeLinkedIn, keyword),
    ]);

    const jobs = dedup([...naukriJobs, ...diceJobs, ...linkedInJobs]);
    console.log(`\n  Total unique jobs for "${keyword}": ${jobs.length}`);

    if (jobs.length === 0) continue;

    // Upsert: update if exists (same title+company+source), insert if new
    const ops = jobs.map((j) => ({
      updateOne: {
        filter: {
          title:   j.title,
          company: j.company,
          source:  j.source,
        },
        update: {
          $set: {
            poc:        j.poc,
            postedDate: j.postedDate,
            email:      j.email,
            mobile:     j.mobile,
            whatsapp:   j.whatsapp,
            linkedin:   j.linkedin,
          },
          // storedAt only written on first insert (current IST timestamp)
          $setOnInsert: {
            storedAt: new Date().toLocaleString("en-IN", {
              timeZone:  "Asia/Kolkata",
              day:       "2-digit",
              month:     "short",
              year:      "numeric",
              hour:      "2-digit",
              minute:    "2-digit",
              second:    "2-digit",
              hour12:    true,
            }) + " IST",
          },
        },
        upsert: true,
      },
    }));

    const result = await Job.bulkWrite(ops);
    totalInserted += result.upsertedCount;
    totalUpdated  += result.modifiedCount;

    console.log(`  Inserted: ${result.upsertedCount} | Updated: ${result.modifiedCount}`);
  }

  // ── Summary ─────────────────────────────────────────────────────────────────
  const allJobs = await Job.find({}).sort({ storedAt: -1 }).lean();

  console.log("\n" + "=".repeat(55));
  console.log(`  Run complete`);
  console.log(`  New jobs inserted : ${totalInserted}`);
  console.log(`  Existing updated  : ${totalUpdated}`);
  console.log(`  Total in DB       : ${allJobs.length}`);
  console.log("=".repeat(55));

  console.log("\nLatest 5 stored records:");
  allJobs.slice(0, 5).forEach((j, i) => {
    console.log(`  ${i + 1}. [${j.source}] ${j.title} @ ${j.company} | ${j.storedAt}`);
  });

  await mongoose.disconnect();
  console.log("\nDisconnected. Done.");
}

harvest().catch((err) => {
  console.error("\nHarvest failed:", err.message);
  process.exit(1);
});
