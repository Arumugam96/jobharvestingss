import React, { useEffect, useMemo, useRef, useState } from "react";
import { useTenant } from "../TenantContext";

/*
 * One-time post-login intro splash. A full-screen overlay that plays the logo
 * float (gradient disc), a spectrum ring drawing itself closed behind a comet
 * dot, and orbiting particles — then DISSOLVES (scale-up cross-fade) to reveal
 * whatever page is underneath. Mounted at the app-shell level (HarvestAgent), so
 * after login it dissolves straight into Harvested Jobs (the home route "/").
 *
 * Plays once per login session (sessionStorage "ss_intro_played", cleared on
 * logout in App.js). Skipped entirely under prefers-reduced-motion.
 */

const PALETTE = [[37, 99, 235], [99, 102, 241], [124, 58, 237], [192, 38, 211], [13, 148, 136]];

function particleColor(t) {
  const n = PALETTE.length;
  const f = ((t % 1) + 1) % 1 * n;
  const i = Math.floor(f) % n, j = (i + 1) % n, k = f - Math.floor(f);
  const a = PALETTE[i], b = PALETTE[j];
  return `${(a[0] + (b[0] - a[0]) * k) | 0},${(a[1] + (b[1] - a[1]) * k) | 0},${(a[2] + (b[2] - a[2]) * k) | 0}`;
}

function IntroParticles() {
  const ref = useRef(null);
  useEffect(() => {
    const cv = ref.current;
    if (!cv) return undefined;
    const ctx = cv.getContext("2d");
    let raf, W, H;
    const resize = () => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      W = cv.clientWidth; H = cv.clientHeight;
      cv.width = W * dpr; cv.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    window.addEventListener("resize", resize);
    const base = Math.min(window.innerWidth, window.innerHeight);
    const N = window.innerWidth < 680 ? 150 : 300;
    const parts = Array.from({ length: N }, () => {
      const orbit = base * 0.18 + Math.random() * base * 0.15;
      return {
        ang: Math.random() * Math.PI * 2, orbit,
        start: orbit + base * 0.4 + Math.random() * base * 0.5,
        twist: (Math.random() * 2 - 1) * 5, dir: Math.random() < 0.5 ? 1 : -1,
        spd: 0.1 + Math.random() * 0.2, size: 0.7 + Math.random() * 1.7,
        bri: 0.5 + Math.random() * 0.5, colT: Math.random(),
      };
    });
    const t0 = performance.now();
    const frame = (now) => {
      const el = now - t0, T = el / 1000, cx = W / 2, cy = H / 2;
      ctx.fillStyle = "rgba(255,255,255,0.17)"; ctx.fillRect(0, 0, W, H);
      const conv = 1 - Math.pow(1 - Math.min(1, el / 1500), 3);
      const appear = Math.min(1, el / 500);
      for (const p of parts) {
        const r = p.start + (p.orbit - p.start) * conv;
        const ang = p.ang + p.twist * (1 - conv) + T * p.spd * p.dir;
        const x = cx + r * Math.cos(ang), y = cy + r * Math.sin(ang);
        const a = p.bri * appear * (0.55 + 0.45 * conv);
        const cc = particleColor(p.colT);
        ctx.fillStyle = `rgba(${cc},${(a * 0.16).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(x, y, p.size * 2.6, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = `rgba(${cc},${(a * 0.9).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(x, y, p.size, 0, Math.PI * 2); ctx.fill();
      }
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", resize); };
  }, []);
  return <canvas ref={ref} className="is-fx" />;
}

export default function IntroSplash() {
  const { tenant, isClient } = useTenant();
  const reducedMotion = useMemo(
    () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    []
  );
  const alreadyPlayed = useMemo(() => {
    try { return sessionStorage.getItem("ss_intro_played") === "1"; } catch { return false; }
  }, []);

  // "intro" (overlay playing) → "hide" (dissolving) → "done" (unmounted).
  const [phase, setPhase] = useState(alreadyPlayed || reducedMotion ? "done" : "intro");
  useEffect(() => {
    if (phase !== "intro") return undefined;
    try { sessionStorage.setItem("ss_intro_played", "1"); } catch { /* ignore */ }
    const t1 = setTimeout(() => setPhase("hide"), 2400);        // begin dissolve
    const t2 = setTimeout(() => setPhase("done"), 2400 + 900);  // then unmount
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, [phase]);

  if (phase === "done") return null;

  return (
    <div className={"is-intro" + (phase === "hide" ? " is-hide" : "")}>
      <style>{CSS}</style>
      <IntroParticles />
      <div className="is-stage">
        <div className="is-logo-block">
          <div className="is-aura" />
          <svg className="is-prog" viewBox="0 0 100 100">
            <defs>
              <linearGradient id="isPg" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="#3B82F6" /><stop offset=".38" stopColor="#7C3AED" />
                <stop offset=".7" stopColor="#C026D3" /><stop offset="1" stopColor="#2DD4BF" />
              </linearGradient>
            </defs>
            <circle className="is-track" cx="50" cy="50" r="47" />
            <circle className="is-arc" cx="50" cy="50" r="47" transform="rotate(-90 50 50)" />
          </svg>
          <div className="is-comet-wrap"><span className="is-comet" /></div>
          <div className="is-floor" />
          <div className="is-float">
            <div className="is-disc">
              <img className="is-logo" src={`${process.env.PUBLIC_URL}/sight_spectrum_logo.jpg`} alt="SightSpectrum" />
            </div>
          </div>
        </div>
        <h1 className="is-brand">SightSpectrum</h1>
        <p className="is-tag">
          {isClient ? `${tenant.name} · Contract Sourcing` : "Contract Sourcing Automation"}
        </p>
        <div className="is-dots"><i /><i /><i /><i /></div>
      </div>
    </div>
  );
}

const CSS = `
.is-intro{position:fixed;inset:0;z-index:60;overflow:hidden;background:radial-gradient(120% 120% at 50% 44%, #FFFFFF 0%, #F7F5FF 52%, #EEF2FF 100%);transition:opacity .85s ease, transform .85s cubic-bezier(.5,0,.2,1);}
.is-intro.is-hide{opacity:0;transform:scale(1.07);pointer-events:none;}
.is-fx{position:absolute;inset:0;width:100%;height:100%;}
.is-stage{position:relative;z-index:3;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:22px;padding:24px;}
.is-logo-block{position:relative;width:230px;height:230px;display:grid;place-items:center;}
.is-aura{position:absolute;width:280px;height:280px;border-radius:50%;background:conic-gradient(from 0deg,#93C5FD,#C4B5FD,#F5D0FE,#99F6E4,#93C5FD);filter:blur(50px);opacity:0;transform-origin:center;animation:isAuraIn 1s ease .1s forwards, isSpin 20s linear infinite;}
.is-prog{position:absolute;width:200px;height:200px;}
.is-prog circle{fill:none;stroke-width:2.6;stroke-linecap:round;}
.is-track{stroke:rgba(100,116,139,.10);}
.is-arc{stroke:url(#isPg);stroke-dasharray:295;stroke-dashoffset:295;animation:isDraw 1.5s cubic-bezier(.32,0,.12,1) .3s forwards;}
.is-comet-wrap{position:absolute;width:200px;height:200px;opacity:0;animation:isSpinDraw 1.5s cubic-bezier(.32,0,.12,1) .3s forwards, isCometFade .4s ease 1.85s forwards;}
.is-comet{position:absolute;top:-2px;left:50%;margin-left:-5px;width:10px;height:10px;border-radius:50%;background:#C026D3;box-shadow:0 0 14px 3px rgba(192,38,211,.65);}
.is-float{position:relative;animation:isFloaty 4.6s ease-in-out 1.4s infinite;}
.is-disc{position:relative;width:146px;height:146px;border-radius:50%;background:radial-gradient(circle at 50% 30%, #FFFFFF 0%, #F2ECFF 72%, #E6DBFF 100%);display:grid;place-items:center;box-shadow:0 22px 48px -20px rgba(124,58,237,.55), 0 4px 14px rgba(30,41,59,.10), 0 0 0 1px rgba(124,58,237,.06);opacity:0;transform:scale(.35) rotate(-135deg);animation:isDiscIn 1.1s cubic-bezier(.18,.9,.24,1) .28s forwards;}
.is-logo{width:100px;height:100px;object-fit:contain;border-radius:50%;mix-blend-mode:multiply;}
.is-floor{position:absolute;bottom:-16px;left:50%;width:110px;height:18px;border-radius:50%;background:rgba(124,58,237,.28);filter:blur(9px);transform:translateX(-50%);animation:isFloor 4.6s ease-in-out 1.4s infinite;}
.is-brand{margin:0;font-size:clamp(30px,6vw,44px);font-weight:800;letter-spacing:-.02em;line-height:1;background:linear-gradient(92deg,#2563EB,#7C3AED,#C026D3,#0D9488);-webkit-background-clip:text;background-clip:text;color:transparent;opacity:0;transform:translateY(16px);animation:isRiseIn .8s cubic-bezier(.2,.8,.2,1) 1.05s forwards;}
.is-tag{margin:0;text-transform:uppercase;letter-spacing:.3em;font-size:12px;color:#64748B;font-weight:700;opacity:0;transform:translateY(12px);animation:isRiseIn .75s ease 1.3s forwards;}
.is-dots{display:flex;gap:7px;opacity:0;animation:isRiseIn .6s ease 1.5s forwards;}
.is-dots i{width:7px;height:7px;border-radius:50%;animation:isBob 1.1s ease-in-out infinite;}
.is-dots i:nth-child(1){background:#2563EB;} .is-dots i:nth-child(2){background:#7C3AED;animation-delay:.15s;}
.is-dots i:nth-child(3){background:#C026D3;animation-delay:.3s;} .is-dots i:nth-child(4){background:#0D9488;animation-delay:.45s;}

@keyframes isSpin{to{transform:rotate(360deg);}}
@keyframes isSpinDraw{from{transform:rotate(0);}to{transform:rotate(360deg);}}
@keyframes isCometFade{to{opacity:0;}}
@keyframes isAuraIn{to{opacity:.55;}}
@keyframes isDraw{to{stroke-dashoffset:0;}}
@keyframes isDiscIn{60%{opacity:1;}100%{opacity:1;transform:scale(1) rotate(0);}}
@keyframes isFloaty{0%,100%{transform:translateY(0);}50%{transform:translateY(-12px);}}
@keyframes isFloor{0%,100%{transform:translateX(-50%) scale(1);opacity:.55;}50%{transform:translateX(-50%) scale(.72);opacity:.3;}}
@keyframes isRiseIn{to{opacity:1;transform:none;}}
@keyframes isBob{0%,100%{transform:translateY(0);opacity:.55;}50%{transform:translateY(-5px);opacity:1;}}

@media (prefers-reduced-motion: reduce){ .is-intro{display:none;} }
`;
