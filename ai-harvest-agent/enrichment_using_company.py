import os
import time
import requests
import pandas as pd
from dotenv import load_dotenv


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(BASE_DIR, ".env"))

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")

if not APOLLO_API_KEY:
    raise RuntimeError(
        "APOLLO_API_KEY was not found in the .env file."
    )

INPUT_FILE = os.path.join(r"C:\Users\SSLTP11173\Downloads", "Harvested_jobs.xlsx")
OUTPUT_FILE = os.path.join(BASE_DIR, "jobs_with_emails.xlsx")

HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "x-api-key": APOLLO_API_KEY,
    "Cache-Control": "no-cache",
}


# ---------------------------------------------------------
# HR titles to search
# ---------------------------------------------------------

HR_TITLES = [
    "HR Manager",
    "Human Resources Manager",
    "Human Resources",
    "HR",
    "Talent Acquisition Manager",
    "Talent Acquisition",
    "Recruiter",
    "Technical Recruiter",
    "Recruitment Manager",
    "Recruitment",
    "Talent Acquisition Specialist",
    "HR Business Partner",
    "Human Resources Business Partner",
]


# ---------------------------------------------------------
# Apollo API helpers
# ---------------------------------------------------------

def find_organization(company_name: str):
    """
    Find an Apollo organization using the company name.
    """

    url = "https://api.apollo.io/api/v1/mixed_companies/search"

    payload = {
        "q_organization_name": company_name,
        "page": 1,
        "per_page": 5,
    }

    try:
        response = requests.post(
            url,
            headers=HEADERS,
            json=payload,
            timeout=30,
        )

        if response.status_code != 200:
            print(
                f"  Organization search failed "
                f"[{response.status_code}]: {response.text[:300]}"
            )
            return None

        data = response.json()

        organizations = data.get("organizations", [])

        if not organizations:
            return None

        # First result for now.
        # We can later add better company-name matching.
        organization = organizations[0]

        return organization

    except requests.RequestException as exc:
        print(f"  Organization request error: {exc}")
        return None


def find_hr_people(organization_id: str):
    """
    Find HR / recruiting people belonging to an Apollo organization.
    """

    url = "https://api.apollo.io/api/v1/mixed_people/api_search"

    payload = {
        "organization_ids": [organization_id],
        "person_titles": HR_TITLES,
        "include_similar_titles": True,
        "contact_email_status": [
            "verified",
            "likely to engage",
            "unverified",
        ],
        "page": 1,
        "per_page": 10,
    }

    try:
        response = requests.post(
            url,
            headers=HEADERS,
            json=payload,
            timeout=30,
        )

        if response.status_code != 200:
            print(
                f"  People search failed "
                f"[{response.status_code}]: {response.text[:300]}"
            )
            return []

        data = response.json()

        return data.get("people", [])

    except requests.RequestException as exc:
        print(f"  People request error: {exc}")
        return []


def enrich_person_email(person_id: str):
    """
    Enrich one Apollo person and retrieve their email.
    """

    url = "https://api.apollo.io/api/v1/people/match"

    params = {
        "reveal_personal_emails": "false",
        "reveal_phone_number": "false",
    }

    payload = {
        "id": person_id,
    }

    try:
        response = requests.post(
            url,
            headers=HEADERS,
            params=params,
            json=payload,
            timeout=30,
        )

        if response.status_code != 200:
            print(
                f"  Person enrichment failed "
                f"[{response.status_code}]: {response.text[:300]}"
            )
            return None

        data = response.json()

        person = data.get("person")

        if not person:
            return None

        email = person.get("email")

        # Apollo may return no usable email.
        if not email:
            return None

        return email

    except requests.RequestException as exc:
        print(f"  Person enrichment error: {exc}")
        return None


# ---------------------------------------------------------
# Find best email for a company
# ---------------------------------------------------------

def find_company_hr_email(company_name: str):
    """
    Complete flow:

        Company name
            ↓
        Apollo organization
            ↓
        HR people
            ↓
        Email enrichment
    """

    print(f"\nCompany: {company_name}")

    organization = find_organization(company_name)

    if not organization:
        print("  No Apollo organization found.")
        return None

    organization_id = organization.get("id")
    organization_name = organization.get("name")

    if not organization_id:
        print("  Organization has no Apollo ID.")
        return None

    print(
        f"  Apollo organization: "
        f"{organization_name} ({organization_id})"
    )

    people = find_hr_people(organization_id)

    if not people:
        print("  No HR/recruiting people found.")
        return None

    print(f"  Found {len(people)} possible HR contacts.")

    # Try people one by one until we find an email.
    for person in people:

        person_id = person.get("id")

        if not person_id:
            continue

        name = person.get("name") or "Unknown"
        title = person.get("title") or "Unknown"

        print(f"    Checking: {name} - {title}")

        email = enrich_person_email(person_id)

        if email:
            print(f"    Email found: {email}")
            return email

        # Small delay between enrichment requests
        time.sleep(0.5)

    print("  No email found for the HR contacts.")

    return None


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():

    print("=" * 60)
    print("Apollo HR Contact Email Finder")
    print("=" * 60)

    if not os.path.exists(INPUT_FILE):
        raise FileNotFoundError(
            f"Excel file not found:\n{INPUT_FILE}"
        )

    # Read Excel
    df = pd.read_excel(INPUT_FILE).head(10)
    
    # Validate Company column
    if "Company" not in df.columns:
        raise ValueError(
            "The Excel file does not contain a 'Company' column."
        )

    # Make sure Email exists
    if "Email" not in df.columns:
        df["Email"] = ""

    # Cache results.
    # If the same company appears multiple times,
    # don't call Apollo repeatedly.
    company_cache = {}

    total_rows = len(df)

    for index, row in df.iterrows():

        company = row.get("Company")

        if pd.isna(company):
            continue

        company = str(company).strip()

        if not company:
            continue

        print(
            f"\n[{index + 1}/{total_rows}] "
            f"Processing {company}"
        )

        # Don't overwrite an existing email
        existing_email = row.get("Email")

        if (
            pd.notna(existing_email)
            and str(existing_email).strip()
        ):
            print(
                f"  Existing email found. Skipping."
            )
            continue

        # Use cached result if this company appeared before
        if company in company_cache:

            email = company_cache[company]

            print(
                f"  Using cached result: "
                f"{email or 'No email'}"
            )

        else:

            email = find_company_hr_email(company)

            company_cache[company] = email

        # Write email
        if email:
            df.at[index, "Email"] = email

        # Save after every company.
        # This prevents losing all progress if the script stops.
        df.to_excel(OUTPUT_FILE, index=False)

        # Small delay to avoid hitting API too aggressively
        time.sleep(1)

    print("\n" + "=" * 60)
    print("Completed")
    print("=" * 60)
    print(f"Output file: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()