import { useState } from "react";
import { createPortal } from "react-dom";
import PhoneVerifyModal, { PhoneVerifyButton, PhoneVerifyStyles } from "./PhoneVerify";
import { logPhoneCall } from "../api";

/* The shield next to a phone number, outside the jobs table: opens the call form
 * and LOGS A NEW CALL (POST /jobs/{id}/phone-verification) — every click adds a new
 * attempt to the job's call history; nothing earlier is overwritten (editing a past
 * call stays on the pencil). The server enforces the self-assignment lock (admins
 * pass) and, as in the table, a call on an unassigned job self-assigns it.
 *
 * Rows that host it are often clickable links (Phone logs), so clicks, keys and
 * mouse-downs inside the button AND the portalled modal are stopped here — React
 * bubbles portal events through the component tree. */

const stop = (e) => e.stopPropagation();

/**
 * @param {object} props
 * @param {string} props.jobId        scraped job the call is logged on (required — no job, no shield)
 * @param {string} props.phone        number as shown
 * @param {string} [props.phoneSource] job | recruiter | contact | company
 * @param {string} [props.pocName]
 * @param {string} [props.pocTitle]
 * @param {string} [props.company]
 * @param {string} [props.email]
 * @param {boolean} [props.called]    the number already has a call (shows the "done" tick)
 * @param {(view: object) => void} [props.onLogged] after a successful save (refresh the page's data)
 */
export default function LogCallShield({
  jobId, phone, phoneSource = "", pocName = "", pocTitle = "", company = "", email = "",
  called = false, onLogged = () => {},
}) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(false);
  if (!jobId || !phone) return null;

  const save = async (call) => {
    const view = await logPhoneCall(jobId, { ...call, phone_source: phoneSource, contact_name: pocName });
    setDone(true);
    onLogged(view);
  };

  return (
    <span className="lcs-wrap" style={{ display: "inline-flex", verticalAlign: "middle", marginLeft: 6 }}
      onClick={stop} onMouseDown={stop} onKeyDown={stop} onAuxClick={stop}>
      <PhoneVerifyStyles />
      <span title={`Log a new call to ${phone}`} style={{ display: "inline-flex" }}>
        <PhoneVerifyButton verified={called || done} onClick={() => setOpen(true)} />
      </span>
      {open && createPortal(
        <div onClick={stop} onMouseDown={stop} onKeyDown={stop} onAuxClick={stop}>
          <PhoneVerifyModal
            jobId={jobId}
            phone={phone}
            pocName={pocName}
            pocTitle={pocTitle}
            company={company}
            email={email}
            onSave={save}
            onClose={() => setOpen(false)}
          />
        </div>,
        document.body,
      )}
    </span>
  );
}
