import mongoose from "mongoose";

const istTimestamp = () =>
  new Date().toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  }) + " IST";

const jobSchema = new mongoose.Schema(
  {
    title:      { type: String, required: true, trim: true },
    company:    { type: String, required: true, trim: true },
    source:     { type: String, enum: ["Naukri", "Dice", "LinkedIn"], required: true },
    poc:        { type: String, default: null },
    postedDate: { type: Date, required: true },
    email:      { type: String, default: null },
    mobile:     { type: String, default: null },
    whatsapp:   { type: String, default: null },
    linkedin:   { type: String, default: null },
    storedAt:   { type: String, default: istTimestamp },
  },
  { collection: "jobs" }
);

export default mongoose.model("Job", jobSchema);
