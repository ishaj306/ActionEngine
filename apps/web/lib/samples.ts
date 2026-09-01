/**
 * Sample documents for the empty state.
 *
 * A first-time user should see what the engine does before deciding whether to
 * hand it something of their own. Both samples are written to exercise the
 * behaviour that distinguishes this product rather than to flatter it: the
 * first hides a prerequisite that takes longer than its deadline allows, and
 * the second is deliberately vague in three places.
 */

export interface Sample {
  id: string;
  name: string;
  summary: string;
  text: string;
}

export const SAMPLES: Sample[] = [
  {
    id: "scholarship",
    name: "Scholarship notice",
    summary: "A hidden prerequisite that no longer fits the deadline",
    text: `NATIONAL MERIT SCHOLARSHIP 2026
Department of Higher Education

Eligible students of the third year must submit the completed application form
along with their income certificate and a self-attested copy of the previous
marksheet to the designated office before 18 September 2026.

Candidates should obtain the income certificate from the Tehsildar's office.
Processing of the certificate is handled by the revenue department.

Applicants must verify their eligibility under the prescribed norms before
applying.

A nominal fee is payable at the time of submission.

Late submissions will not be entertained under any circumstances.`,
  },
  {
    id: "internship",
    name: "Internship registration",
    summary: "A relative deadline and an unnamed submission portal",
    text: `NOTICE — INTERNSHIP REGISTRATION (2026 CYCLE)
Training and Placement Cell

All third-year students of Computer Science are required to register for the
internship programme through the online portal.

Students must upload their updated resume, a scanned copy of the college
identity card, and the internship offer letter issued by the host organisation.

Candidates should collect the offer letter from the host organisation before
uploading it.

Registrations must be completed within 10 days of the publication of this
notice.

Queries may be directed to the concerned office during working hours.`,
  },
];
