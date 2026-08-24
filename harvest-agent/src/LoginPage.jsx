import React, { useState, useRef, useEffect, useCallback } from "react";
import {
  Mail,
  ArrowLeft,
  ArrowRight,
  RefreshCw,
  Loader2,
  CheckCircle2,
  AlertCircle,
  ShieldCheck,
} from "lucide-react";
import { requestOtp, verifyOtp, ApiError } from "./api";

/* ------------------------------------------------------------------ */
/* config                                                              */
/* ------------------------------------------------------------------ */
const OTP_LENGTH = 6;
const RESEND_SECONDS = 60; // matches backend otp_resend_cooldown_seconds
const MAX_RESENDS = 3;
const MAX_ATTEMPTS = 5; // matches backend otp_max_attempts

// Only Sightspectrum emails may log in — any single-label TLD (.com/.in/.org/.io…),
// mirroring the backend validator (validate_company_email). Client-side check is
// just instant feedback; the backend is authoritative (422 otherwise).
const EMAIL_RE = /^[^\s@]+@sightspectrum\.[a-z]{2,}$/i;

/* ------------------------------------------------------------------ */
/* styles                                                              */
/* ------------------------------------------------------------------ */
const CSS = `
:root{
  --primary:#2563EB; --secondary:#1E40AF; --accent:#F59E0B;
  --bg:#F8FAFC; --text:#1E293B;
  --muted:#64748B; --line:#E2E8F0; --surface:#FFFFFF;
  --ok:#16A34A; --err:#DC2626;
}
*{box-sizing:border-box}
.auth-root{
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:32px 20px;background:var(--bg);color:var(--text);
  font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.auth-card{width:100%;max-width:392px}
.auth-box{
  background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:32px 30px;box-shadow:0 1px 2px rgba(15,23,42,.04);
}

/* ---------- wordmark ---------- */
.wordmark{
  text-align:center;font-size:15px;font-weight:700;letter-spacing:-.2px;
  color:var(--text);margin:0 0 18px;
}
.wordmark span{color:var(--primary)}

/* ---------- header ---------- */
.card-icon{
  width:42px;height:42px;border-radius:11px;display:grid;place-items:center;
  background:#EFF6FF;color:var(--primary);margin-bottom:18px;
}
.card-title{font-size:21px;font-weight:700;letter-spacing:-.4px;margin:0 0 7px}
.card-head{
  display:flex;align-items:center;justify-content:center;gap:11px;margin-bottom:22px;
}
.card-head .card-icon{margin-bottom:0;width:38px;height:38px;border-radius:10px}
.card-head .card-title{margin:0}
.card-head.tight{margin-bottom:12px}
.card-sub.centered{text-align:center}
.card-sub{font-size:13.5px;line-height:1.6;color:var(--muted);margin:0 0 24px}
.card-sub strong{color:var(--text);font-weight:600}

/* ---------- field ---------- */
.field{margin-bottom:18px}
.label{display:block;font-size:12.5px;font-weight:600;margin-bottom:7px}
.input-wrap{position:relative}
.input-wrap>svg{
  position:absolute;left:13px;top:50%;transform:translateY(-50%);
  color:#94A3B8;pointer-events:none;
}
.input{
  width:100%;height:44px;padding:0 14px 0 40px;font-size:14px;color:var(--text);
  background:var(--surface);border:1px solid var(--line);border-radius:9px;outline:none;
  transition:border-color .15s,box-shadow .15s;font-family:inherit;
}
.input::placeholder{color:#94A3B8}
.input:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(37,99,235,.13)}
.input.is-err{border-color:var(--err)}
.input.is-err:focus{box-shadow:0 0 0 3px rgba(220,38,38,.12)}
.field-hint{font-size:12px;color:var(--muted);margin-top:7px}

/* ---------- otp cells ---------- */
.otp-row{display:flex;gap:8px}
.otp-cell{
  flex:1;min-width:0;height:52px;text-align:center;
  font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--text);
  background:var(--surface);border:1px solid var(--line);border-radius:9px;outline:none;
  transition:border-color .15s,box-shadow .15s,background .15s;font-family:inherit;
}
.otp-cell:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(37,99,235,.13)}
.otp-cell.filled{border-color:#BFD4FE;background:#F8FAFF}
.otp-cell.is-err{border-color:var(--err);background:#FEF2F2}
.otp-shake{animation:shake .32s ease}
@keyframes shake{
  0%,100%{transform:translateX(0)} 20%{transform:translateX(-5px)}
  40%{transform:translateX(5px)} 60%{transform:translateX(-3px)} 80%{transform:translateX(3px)}
}

/* ---------- messages ---------- */
.msg{display:flex;align-items:flex-start;gap:7px;font-size:12.5px;line-height:1.5;margin-top:9px}
.msg.err{color:var(--err)}
.msg.ok{color:var(--ok)}
.msg svg{flex:0 0 14px;margin-top:1px}

/* ---------- buttons ---------- */
.btn{
  width:100%;height:44px;display:inline-flex;align-items:center;justify-content:center;gap:8px;
  font-size:14px;font-weight:600;font-family:inherit;border:1px solid transparent;border-radius:9px;
  cursor:pointer;transition:background .15s,opacity .15s;
}
.btn-primary{background:var(--primary);color:#fff}
.btn-primary:hover:not(:disabled){background:var(--secondary)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn:focus-visible{outline:2px solid var(--primary);outline-offset:2px}

.resend-bar{
  display:flex;align-items:center;justify-content:space-between;gap:12px;
  margin-top:18px;padding-top:16px;border-top:1px solid var(--line);
}
.resend-note{font-size:12.5px;color:var(--muted)}
.link-btn{
  display:inline-flex;align-items:center;gap:6px;background:none;border:none;padding:0;
  font-size:12.5px;font-weight:600;font-family:inherit;color:var(--primary);cursor:pointer;
}
.link-btn:hover:not(:disabled){color:var(--secondary);text-decoration:underline}
.link-btn:disabled{color:#94A3B8;cursor:not-allowed;text-decoration:none}
.link-btn.back{color:var(--muted);margin-bottom:18px}
.link-btn.back:hover{color:var(--text);text-decoration:none}
.spin{animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

@media (prefers-reduced-motion:reduce){*{animation:none !important;transition:none !important}}
@media (max-width:420px){
  .auth-box{padding:26px 20px}
  .otp-row{gap:6px}
  .otp-cell{height:48px;font-size:18px}
}
`;

/* ------------------------------------------------------------------ */
/* component                                                           */
/* ------------------------------------------------------------------ */
export default function LoginPage({ onAuthenticated }) {
  const [step, setStep] = useState("email"); // email | otp
  const [email, setEmail] = useState("");
  const [digits, setDigits] = useState(Array(OTP_LENGTH).fill(""));
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [shake, setShake] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [resends, setResends] = useState(0);
  const [attempts, setAttempts] = useState(0);

  const cellRefs = useRef([]);

  /* resend countdown */
  useEffect(() => {
    if (cooldown <= 0) return undefined;
    const t = setTimeout(() => setCooldown((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  /* focus first cell when the code step opens */
  useEffect(() => {
    if (step === "otp") cellRefs.current[0]?.focus();
  }, [step]);

  const code = digits.join("");
  const emailValid = EMAIL_RE.test(email.trim());

  /* ---------------- send / resend ---------------- */
  const sendCode = useCallback(
    async (isResend) => {
      setError("");
      setNotice("");
      setBusy(true);
      try {
        await requestOtp(email.trim());
      } catch (err) {
        setBusy(false);
        if (err instanceof ApiError && err.status === 429) {
          // Server-enforced resend cooldown. Retry-After is an HTTP header the
          // api client doesn't surface, so fall back to the known cooldown.
          setCooldown(RESEND_SECONDS);
          setError(err.message || `Please wait before requesting another code.`);
          return;
        }
        setError(
          err instanceof ApiError
            ? err.message
            : "Could not send the code — check your connection and try again."
        );
        return;
      }
      setBusy(false);
      setCooldown(RESEND_SECONDS);
      setDigits(Array(OTP_LENGTH).fill(""));
      if (isResend) {
        setResends((n) => n + 1);
        setAttempts(0);
        setNotice("New code sent. The previous one no longer works.");
        cellRefs.current[0]?.focus();
      } else {
        setStep("otp");
      }
    },
    [email]
  );

  const handleEmailSubmit = () => {
    if (!emailValid) {
      setError("Enter your Sightspectrum email address.");
      return;
    }
    sendCode(false);
  };

  /* ---------------- otp entry ---------------- */
  const writeDigit = (i, val) => {
    const next = [...digits];
    next[i] = val;
    setDigits(next);
    return next;
  };

  const handleCellChange = (i, raw) => {
    const val = raw.replace(/\D/g, "");
    if (!val) {
      writeDigit(i, "");
      return;
    }
    if (val.length > 1) {
      const next = [...digits];
      val.split("").slice(0, OTP_LENGTH - i).forEach((d, k) => {
        next[i + k] = d;
      });
      setDigits(next);
      setError("");
      cellRefs.current[Math.min(i + val.length, OTP_LENGTH - 1)]?.focus();
      return;
    }
    writeDigit(i, val);
    setError("");
    if (i < OTP_LENGTH - 1) cellRefs.current[i + 1]?.focus();
  };

  const handleCellKey = (i, e) => {
    if (e.key === "Backspace" && !digits[i] && i > 0) {
      e.preventDefault();
      writeDigit(i - 1, "");
      cellRefs.current[i - 1]?.focus();
    } else if (e.key === "ArrowLeft" && i > 0) {
      e.preventDefault();
      cellRefs.current[i - 1]?.focus();
    } else if (e.key === "ArrowRight" && i < OTP_LENGTH - 1) {
      e.preventDefault();
      cellRefs.current[i + 1]?.focus();
    } else if (e.key === "Enter" && code.length === OTP_LENGTH) {
      verifyCode();
    }
  };

  const handlePaste = (e) => {
    const text = (e.clipboardData.getData("text") || "").replace(/\D/g, "");
    if (!text) return;
    e.preventDefault();
    const next = Array(OTP_LENGTH).fill("");
    text.split("").slice(0, OTP_LENGTH).forEach((d, k) => {
      next[k] = d;
    });
    setDigits(next);
    setError("");
    cellRefs.current[Math.min(text.length, OTP_LENGTH) - 1]?.focus();
  };

  /* ---------------- verify ---------------- */
  const verifyCode = async () => {
    if (code.length !== OTP_LENGTH || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      // On success the backend sets the Secure HttpOnly session cookie — there's
      // no token to stash in JS. The returned access_token is intentionally
      // ignored; the app authenticates via the cookie from here on.
      await verifyOtp(email.trim(), code);
      setBusy(false);
      onAuthenticated?.({ email: email.trim() });
      return;
    } catch (err) {
      setBusy(false);
      setShake(true);
      setTimeout(() => setShake(false), 340);
      setDigits(Array(OTP_LENGTH).fill(""));

      // Anything other than the expected generic 401 (wrong/expired/consumed/
      // too-many) is surfaced verbatim.
      if (err instanceof ApiError && err.status !== 401) {
        setError(err.message);
        cellRefs.current[0]?.focus();
        return;
      }

      // Backend can't tell us how many tries remain (single generic 401), so we
      // track locally for messaging only; the server is the real authority.
      const used = attempts + 1;
      setAttempts(used);
      if (used >= MAX_ATTEMPTS) {
        setError("Too many incorrect codes. Request a new one to continue.");
        return;
      }
      const left = MAX_ATTEMPTS - used;
      setError(`That code is incorrect or expired. ${left} ${left === 1 ? "try" : "tries"} left.`);
      cellRefs.current[0]?.focus();
    }
  };

  const locked = attempts >= MAX_ATTEMPTS;
  const resendBlocked = cooldown > 0 || resends >= MAX_RESENDS || busy;

  const goBack = () => {
    setStep("email");
    setDigits(Array(OTP_LENGTH).fill(""));
    setError("");
    setNotice("");
    setAttempts(0);
    setResends(0);
    setCooldown(0);
  };

  /* ---------------- render ---------------- */
  return (
    <div className="auth-root">
      <style>{CSS}</style>

      <div className="auth-card">
        <p className="wordmark">
          Harvest<span>Agent</span>
        </p>

        {/* ---------------- step: email ---------------- */}
        {step === "email" && (
          <div className="auth-box">
            <div className="card-head">
              <div className="card-icon">
                <Mail size={20} strokeWidth={2.2} />
              </div>
              <h2 className="card-title">Sign in</h2>
            </div>

            <div className="field">
              <label className="label" htmlFor="auth-email">
                Work email
              </label>
              <div className="input-wrap">
                <Mail size={16} />
                <input
                  id="auth-email"
                  className={`input${error ? " is-err" : ""}`}
                  type="email"
                  autoComplete="email"
                  autoFocus
                  placeholder="you@sightspectrum..."
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    setError("");
                  }}
                  onKeyDown={(e) => e.key === "Enter" && handleEmailSubmit()}
                />
              </div>
              {error ? (
                <div className="msg err">
                  <AlertCircle size={14} />
                  <span>{error}</span>
                </div>
              ) : (
                <div className="field-hint">Use your Sightspectrum email.</div>
              )}
            </div>

            <button
              className="btn btn-primary"
              onClick={handleEmailSubmit}
              disabled={busy || !email.trim()}
            >
              {busy ? (
                <>
                  <Loader2 size={16} className="spin" /> Sending code
                </>
              ) : (
                <>
                  Send code <ArrowRight size={16} />
                </>
              )}
            </button>
          </div>
        )}

        {/* ---------------- step: otp ---------------- */}
        {step === "otp" && (
          <div className="auth-box">
            <button className="link-btn back" onClick={goBack}>
              <ArrowLeft size={14} /> Use a different email
            </button>

            <div className="card-head tight">
              <div className="card-icon">
                <ShieldCheck size={20} strokeWidth={2.2} />
              </div>
              <h2 className="card-title">Enter your code</h2>
            </div>
            <p className="card-sub centered">
              Sent to <strong>{email.trim()}</strong>. It expires in 5 minutes.
            </p>

            <div className={`otp-row${shake ? " otp-shake" : ""}`} onPaste={handlePaste}>
              {digits.map((d, i) => (
                <input
                  key={i}
                  ref={(el) => {
                    cellRefs.current[i] = el;
                  }}
                  className={`otp-cell${d ? " filled" : ""}${error ? " is-err" : ""}`}
                  type="text"
                  inputMode="numeric"
                  autoComplete={i === 0 ? "one-time-code" : "off"}
                  maxLength={OTP_LENGTH}
                  aria-label={`Digit ${i + 1}`}
                  value={d}
                  disabled={busy || locked}
                  onChange={(e) => handleCellChange(i, e.target.value)}
                  onKeyDown={(e) => handleCellKey(i, e)}
                  onFocus={(e) => e.target.select()}
                />
              ))}
            </div>

            {error && (
              <div className="msg err">
                <AlertCircle size={14} />
                <span>{error}</span>
              </div>
            )}
            {!error && notice && (
              <div className="msg ok">
                <CheckCircle2 size={14} />
                <span>{notice}</span>
              </div>
            )}

            <div style={{ marginTop: 20 }}>
              <button
                className="btn btn-primary"
                onClick={verifyCode}
                disabled={busy || code.length !== OTP_LENGTH || locked}
              >
                {busy ? (
                  <>
                    <Loader2 size={16} className="spin" /> Verifying
                  </>
                ) : (
                  <>
                    Verify and sign in <ArrowRight size={16} />
                  </>
                )}
              </button>
            </div>

            <div className="resend-bar">
              <span className="resend-note">
                {resends >= MAX_RESENDS
                  ? "Resend limit reached — contact your admin."
                  : cooldown > 0
                  ? `You can resend in ${cooldown}s`
                  : "Didn't get the email?"}
              </span>
              <button
                className="link-btn"
                onClick={() => sendCode(true)}
                disabled={resendBlocked}
              >
                <RefreshCw size={13} className={busy ? "spin" : ""} />
                Resend code
                {resends > 0 && resends < MAX_RESENDS ? ` (${MAX_RESENDS - resends} left)` : ""}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
