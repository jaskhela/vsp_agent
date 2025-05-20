import streamlit as st
import pdfplumber
import re
import pandas as pd

# ─── Parsing logic ─────────────────────────────────────────────────────────────

def merge_headers(lines):
    plan_start_pattern = re.compile(r"^(CHOICE|SIG PLAN|SIG\s+PLAN|EXAMON|ADVTG|DVINS)", re.IGNORECASE)
    merged, i = [], 0
    while i < len(lines):
        ln = lines[i].strip()
        if not ln:
            merged.append(ln); i += 1; continue
        if plan_start_pattern.match(ln):
            combined = ln
            while True:
                if i + 1 >= len(lines): break
                nxt = lines[i + 1].strip()
                if plan_start_pattern.match(nxt) or nxt.startswith("Totals") or is_detail_line(nxt) or "VSP Vision Care" in nxt:
                    break
                i += 1
                combined += " " + nxt
            merged.append(combined.strip())
        else:
            merged.append(ln)
        i += 1
    return merged


def is_detail_line(line):
    detail_regex = (
        r"^(?:[A-Z]\s+)?"
        r"(?:(\d{1,2}/\d{1,2}/\d{2}))?"
        r"\s*([A-Za-z0-9]+)"
        r"(?:\s+([A-Za-z0-9]+))?\s+"
        r"(\d+)\s+"
        r"(.*?)\s+"
        r"([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"
    )
    return bool(re.match(detail_regex, line.strip()))


def parse_claim_header(line):
    tokens = line.split()
    if not tokens:
        return {"plan":"","insured_id":"","patient_name":"","pt_acct":"","claim_number":""}
    first = tokens[0].upper()
    if first == "SIG" and len(tokens) > 1 and tokens[1].upper() == "PLAN":
        plan, idx = "SIG PLAN", 2
    else:
        plan, idx = tokens[0], 1
    if idx >= len(tokens):
        return {"plan":plan,"insured_id":"","patient_name":"","pt_acct":"","claim_number":""}
    tok = tokens[idx]
    m = re.match(r"^([A-Za-z]+)(\d+)(.*)$", tok)
    if m:
        insured_id = m.group(1) + m.group(2)
        extra = m.group(3).strip()
    else:
        insured_id, extra = tok, ""
    idx += 1
    patient, claim = ([extra] if extra else []), ""
    for t in tokens[idx:]:
        if not claim and t.isdigit():
            claim = t
        else:
            patient.append(t)
    return {
        "plan": plan,
        "insured_id": insured_id,
        "patient_name": " ".join(patient).strip(),
        "pt_acct": "",
        "claim_number": claim,
    }


def parse_detail_line(line):
    detail_regex = (
        r"^(?:[A-Z]\s+)?"
        r"(?:(\d{1,2}/\d{1,2}/\d{2}))?"
        r"\s*([A-Za-z0-9]+)"
        r"(?:\s+([A-Za-z0-9]+))?\s+"
        r"(\d+)\s+"
        r"(.*?)\s+"
        r"([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"
        r"(?:\s+(.*))?$"
    )
    m = re.match(detail_regex, line.strip())
    if not m:
        return {"raw_line": line}
    (
        service_date, proc, opt_mod, units, desc,
        billed, tot_comp, copay, pay_mat, prov_mat,
        prov_pay, msg_codes
    ) = m.groups()
    proc_mod = f"{proc} {opt_mod}" if opt_mod else proc
    return {
        "service_date": service_date or "",
        "proc_code_mod": proc_mod,
        "units": units,
        "service_description": desc.strip(),
        "billed_amount": billed,
        "total_compensation": tot_comp,
        "copay": copay,
        "patient_pay_materials": pay_mat,
        "plan_provided_materials": prov_mat,
        "provider_payment": prov_pay,
        "message_codes": msg_codes.strip() if msg_codes else ""
    }


def extract_message_code_definitions(pdf_path):
    defs = {}
    code_pattern = re.compile(r"^([A-Z0-9]{2,6})\s*[:\-]\s*(.+)$")
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").split("\n"):
                m = code_pattern.match(line.strip())
                if m:
                    code, desc = m.groups()
                    defs[code] = desc.strip()
    return defs


def extract_claims(pdf_path):
    claims, cur = [], None
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            lines = (page.extract_text() or "").split("\n")
            lines = merge_headers(lines)
            for ln in lines:
                txt = ln.strip()
                if not txt: continue
                if re.match(r"^(CHOICE|SIG PLAN|EXAMON|ADVTG|DVINS)", txt, re.IGNORECASE):
                    if cur:
                        claims.append(cur)
                    hdr = parse_claim_header(txt)
                    cur = {"header": hdr, "details": [], "totals_line": None}
                    continue
                if txt.startswith("Totals") and cur:
                    cur["totals_line"] = txt
                    claims.append(cur)
                    cur = None
                    continue
                if cur:
                    cur["details"].append(parse_detail_line(txt))
    if cur:
        claims.append(cur)
    return claims


def claims_to_dataframe(claims, code_defs=None):
    rows = []
    for c in claims:
        hdr = c["header"]
        default = next((d["service_date"] for d in c["details"] if d.get("service_date")), "")
        for d in c["details"]:
            if "raw_line" in d: continue
            date = d["service_date"] or default
            codes = d["message_codes"]
            descs = []
            if code_defs:
                descs = [code_defs.get(code, "") for code in codes.split() if code]
            code_desc = "; ".join([d for d in descs if d])
            rows.append({
                "Plan": hdr.get("plan",""),
                "Insured ID": hdr.get("insured_id",""),
                "Patient Name": hdr.get("patient_name",""),
                "Message Codes": codes,
                "Message Code Descriptions": code_desc,
                "Pt Acct #": hdr.get("pt_acct",""),
                "Claim #": hdr.get("claim_number",""),
                "Date": date,
                "Proc": d["proc_code_mod"],
                "Desc": d["service_description"],
                "Billed": d["billed_amount"],
                "TotalComp": d["total_compensation"],
                "Copay": d["copay"],
                "Patient Pay Materials": d["patient_pay_materials"],
                "ProvMat": d["plan_provided_materials"],
                "ProvPay": d["provider_payment"]
            })
    return pd.DataFrame(rows)

# ─── Streamlit UI ───────────────────────────────────────────────────────────────

st.title("Claims PDF → Table with Highlights and Stats")

uploaded = st.file_uploader("Upload one or more EOP PDF(s)", type="pdf", accept_multiple_files=True)
if uploaded:
    all_dfs = []
    for pdf_file in uploaded:
        st.write(f"**Processing:** {pdf_file.name}")
        claims = extract_claims(pdf_file)
        code_defs = extract_message_code_definitions(pdf_file)
        df = claims_to_dataframe(claims, code_defs)
        all_dfs.append(df)

    result_df = pd.concat(all_dfs, ignore_index=True)

    # Reorder columns
    cols = [
        "Plan", "Insured ID", "Patient Name",
        "Message Codes", "Message Code Descriptions",
        "Pt Acct #", "Claim #", "Date", "Proc", "Desc",
        "Billed", "TotalComp", "Copay", "Patient Pay Materials",
        "ProvMat", "ProvPay"
    ]
    result_df = result_df[cols]

    # sort so rows with message codes come first
    result_df["has_codes"] = result_df["Message Codes"].astype(bool)
    result_df = result_df.sort_values("has_codes", ascending=False).drop(columns=["has_codes"])

    # Compute summary stats
    total = len(result_df)
    with_codes = result_df["Message Codes"].astype(bool).sum()
    without_codes = total - with_codes
    c1, c2, c3 = st.columns(3)
    c1.metric("Total claims processed", total)
    c2.metric("Claims needing attention", with_codes)
    c3.metric("Claims paid out", without_codes)

    # Highlight rows with message codes
    def highlight_attention(row):
        return ['background-color: #FFCCCC' if row['Message Codes'] else '' for _ in row]
    styled = result_df.style.apply(highlight_attention, axis=1)

    st.subheader("Parsed Claims")
    st.dataframe(styled, use_container_width=True)

    # Download button
    csv = result_df.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", data=csv, file_name="parsed_claims.csv")
