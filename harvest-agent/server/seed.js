import "dotenv/config";
import mongoose from "mongoose";
import Job from "./models/Job.js";

// ── Connection ────────────────────────────────────────────────────────────────
const MONGO_URI = process.env.MONGO_URI || "mongodb://localhost:27017/harvest_agent";

// ── Seed data (mirrors HarvestAgent.jsx exactly) ─────────────────────────────
// Columns: [title, company, poc, postedHrs, email, whatsapp, linkedin, mobile]
const seed = [
  ["Sr. React Developer",        "HDFC Bank",   "Anita Rao",    2,  "anita.rao@hdfcbank.com",  "+919811023145", "https://linkedin.com/in/anita-rao",      "+919811023145"],
  ["Data Engineer – Azure",      "Cognizant",   "Suresh Iyer",  5,  null,                      "+919900145872", "https://linkedin.com/in/suresh-iyer",    "+919900145872"],
  ["Product Owner – Fintech",    "PhonePe",     "Rahul Mehta",  18, "rahul.m@phonepe.com",     null,            "https://linkedin.com/in/rahul-mehta",    "+919876501234"],
  ["DevOps Engineer",            "Infosys",     "Sneha Kapoor", 1,  "sneha.k@infosys.com",     "+919845512367", null,                                     "+919845512367"],
  ["Java Backend Lead",          "ICICI Bank",  "Vikram Singh", 7,  "vikram.s@icicibank.com",  null,            null,                                     "+919812000456"],
  ["ML Engineer",                "Flipkart",    null,           3,  null,                      null,            "https://linkedin.com/in/flipkart-talent", null],
  ["QA Automation Specialist",   "Wipro",       "Deepa Rao",    26, null,                      "+919823004567", null,                                     "+919823004567"],
  ["Salesforce Developer",       "Accenture",   "Karan Thapar", 9,  "karan.t@accenture.com",  "+919812234509", "https://linkedin.com/in/karan-thapar",   "+919812234509"],
  ["Frontend Engineer – Angular","Swiggy",      "Megha Das",    4,  "megha.d@swiggy.in",       null,            "https://linkedin.com/in/megha-das",      "+919800123987"],
  ["Cloud Architect",            "TCS",         null,           31, null,                      null,            null,                                     null],
  ["iOS Developer",              "Razorpay",    "Arjun Pillai", 6,  "arjun.p@razorpay.com",   "+919900781245", null,                                     "+919900781245"],
  ["Business Analyst",           "Deloitte",    "Nisha Reddy",  12, "nisha.r@deloitte.com",   "+919811556230", "https://linkedin.com/in/nisha-reddy",    "+919811556230"],
  ["Scala Developer",            "Walmart",     "Sanjay Gupta", 8,  "sanjay.g@walmart.com",   null,            "https://linkedin.com/in/sanjay-gupta",   "+919812778345"],
  ["Site Reliability Engineer",  "Paytm",       "Tanvi Bose",   14, null,                      "+919822119988", "https://linkedin.com/in/tanvi-bose",     "+919822119988"],
  ["Power BI Developer",         "Genpact",     null,           22, null,                      null,            "https://linkedin.com/in/genpact-hr",     null],
  [".NET Full Stack",            "Capgemini",   "Rohit Verma",  10, "rohit.v@capgemini.com",  "+919845009921", null,                                     "+919845009921"],
  ["UX Designer",                "CRED",        "Aditi Sharma", 5,  "aditi.s@cred.club",      null,            null,                                     "+919811223344"],
  ["Security Engineer",          "Zerodha",     "Ramesh Kumar", 16, "ramesh.k@zerodha.com",   "+919900456712", "https://linkedin.com/in/ramesh-kumar",   "+919900456712"],
];

const SOURCES = [
  "Naukri","Dice","LinkedIn","Naukri","Naukri","LinkedIn",
  "Dice","LinkedIn","Naukri","Dice","LinkedIn","Naukri",
  "LinkedIn","Dice","Naukri","LinkedIn","Naukri","Dice",
];

// Base harvest time: Run #24 · 26 Jun 2026 09:15 AM
const BASE = new Date(2026, 5, 26, 9, 15).getTime();

const docs = seed.map(([title, company, poc, postedHrs, email, whatsapp, linkedin, mobile], i) => ({
  title,
  company,
  source:     SOURCES[i],
  poc:        poc   ?? null,
  postedDate: new Date(BASE - postedHrs * 3600 * 1000),
  email:      email    ?? null,
  mobile:     mobile   ?? null,
  whatsapp:   whatsapp ?? null,
  linkedin:   linkedin ?? null,
  // storedAt is auto-set by the schema default (current IST timestamp)
}));

// ── Run ───────────────────────────────────────────────────────────────────────
async function run() {
  await mongoose.connect(MONGO_URI);
  console.log(`Connected to MongoDB: ${MONGO_URI}`);

  await Job.deleteMany({});
  console.log("Cleared existing jobs collection.");

  const inserted = await Job.insertMany(docs);
  console.log(`\nInserted ${inserted.length} jobs:\n`);

  inserted.forEach((j) => {
    console.log(`  [${j._id}] ${j.title} @ ${j.company} | storedAt: ${j.storedAt}`);
  });

  await mongoose.disconnect();
  console.log("\nDone. Disconnected from MongoDB.");
}

run().catch((err) => {
  console.error("Seed failed:", err.message);
  process.exit(1);
});
